from typing import TypeVar, Generic, Callable, Awaitable, AsyncIterable, Sequence, overload
from dataclasses import dataclass
from datetime import datetime
from haskellian import asyn_iter as AI, iter as I, either as E, Either, Right, promise as P
from kv.api import LocatableKV, InvalidData, DBError
from kv.azure.blob import BlobContainerKV
from azure.storage.blob.aio import BlobServiceClient

A = TypeVar('A')

def default_split(key: str) -> tuple[str, str]:
  parts = key.split('/', 1)
  return (parts[0], parts[1]) if len(parts) > 1 else ('container', parts[0])

@dataclass
class BlobKV(LocatableKV[A], Generic[A]):
  client: Callable[[], BlobServiceClient]
  split_key: Callable[[str], tuple[str, str]] = default_split
  """Split a key into container + blob. Defaults to `{container}/{blob/with/slashes}`"""
  parse: Callable[[bytes], Either[InvalidData, A]] = Right # type: ignore
  dump: Callable[[A], bytes | str] = lambda x: x # type: ignore

  @overload
  @classmethod
  def validated(
    cls, Type: type[A], conn_str: str, /,
    split_key: Callable[[str], tuple[str, str]] = default_split
  ) -> 'BlobKV[A]':
    ...
  @overload
  @classmethod
  def validated(
    cls, Type: type[A], client: Callable[[], BlobServiceClient], /,
    split_key: Callable[[str], tuple[str, str]] = default_split
  ) -> 'BlobKV[A]':
    ...

  @classmethod
  def validated(
    cls, Type: type[A], client: Callable[[], BlobServiceClient] | str,
    split_key: Callable[[str], tuple[str, str]] = default_split
  ) -> 'BlobKV[A]':
  
    if isinstance(client, str):
      client = lambda: BlobServiceClient.from_connection_string(client)
    from pydantic import RootModel
    Model = RootModel[Type]
    return BlobKV(
      client=client, split_key=split_key,
      parse=lambda b: E.validate_json(b, Model).fmap(lambda x: x.root).mapl(InvalidData),
      dump=lambda x: Model(x).model_dump_json(exclude_none=True)
    )
  
  @classmethod
  def from_conn_str(
    cls, conn_str: str,
    split_key: Callable[[str], tuple[str, str]] = default_split
  ) -> 'BlobKV[A]':
    return BlobKV(lambda: BlobServiceClient.from_connection_string(conn_str), split_key=split_key)

  def _kv(self, container: str) -> BlobContainerKV:
    return BlobContainerKV(
      client=self.client, container=container,
      parse=self.parse, dump=self.dump
    )

  def _delete(self, key: str):
    container, blob = self.split_key(key)
    return self._kv(container)._delete(blob)
  
  def _insert(self, key: str, value: A):
    container, blob = self.split_key(key)
    return self._kv(container)._insert(blob, value)
  
  def _read(self, key: str):
    container, blob = self.split_key(key)
    return self._kv(container)._read(blob)
  
  async def _containers(self) -> Sequence[str]:
    async with self.client() as client:
      return [c.name async for c in client.list_containers()] # type: ignore
  
  @E.do[DBError]()
  async def _container_keys(self, container: str):
    keys = (await self._kv(container).keys()).unsafe()
    return [f"{container}/{k}" for k in keys]

  async def _keys(self) -> Either[DBError, Sequence[str]]:
    tasks: list[Awaitable[Either[DBError, Sequence[str]]]] = []
    for container in await self._containers():
      tasks.append(self._container_keys(container))
    results = E.sequence(await P.all(tasks))
    return results.mapl(DBError) | I.flatten | list
  
  async def _items(self, batch_size: int | None = None) -> AsyncIterable[Either[DBError | InvalidData, tuple[str, A]]]:
    for container in await self._containers():
      async for item in self._kv(container).items(batch_size):
        yield item

  def url(self, key: str, *, expiry: datetime | None = None) -> str:
    container, blob = self.split_key(key)
    return self._kv(container).url(blob, expiry=expiry)