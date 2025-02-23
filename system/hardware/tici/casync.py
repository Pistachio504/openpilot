#!/usr/bin/env python3
import io
import lzma
import os
import struct
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict, namedtuple
from typing import Callable, Dict, List, Optional, Tuple

import requests
from Crypto.Hash import SHA512

CA_FORMAT_INDEX = 0x96824d9c7b129ff9
CA_FORMAT_TABLE = 0xe75b9e112f17417d
CA_FORMAT_TABLE_TAIL_MARKER = 0xe75b9e112f17417
FLAGS = 0xb000000000000000

CA_HEADER_LEN = 48
CA_TABLE_HEADER_LEN = 16
CA_TABLE_ENTRY_LEN = 40
CA_TABLE_MIN_LEN = CA_TABLE_HEADER_LEN + CA_TABLE_ENTRY_LEN

CHUNK_DOWNLOAD_TIMEOUT = 10
CHUNK_DOWNLOAD_RETRIES = 3

CAIBX_DOWNLOAD_TIMEOUT = 120

Chunk = namedtuple('Chunk', ['sha', 'offset', 'length'])
ChunkDict = Dict[bytes, Chunk]


class ChunkReader(ABC):
  @abstractmethod
  def read(self, chunk: Chunk) -> bytes:
    ...


class FileChunkReader(ChunkReader):
  """Reads chunks from a local file"""
  def __init__(self, fn: str) -> None:

    super().__init__()
    self.f = open(fn, 'rb')

  def read(self, chunk: Chunk) -> bytes:
    self.f.seek(chunk.offset)
    return self.f.read(chunk.length)


class RemoteChunkReader(ChunkReader):
  """Reads lzma compressed chunks from a remote store"""

  def __init__(self, url: str) -> None:
    super().__init__()
    self.url = url

  def read(self, chunk: Chunk) -> bytes:
    sha_hex = chunk.sha.hex()
    url = os.path.join(self.url, sha_hex[:4], sha_hex + ".cacnk")

    for i in range(CHUNK_DOWNLOAD_RETRIES):
      try:
        resp = requests.get(url, timeout=CHUNK_DOWNLOAD_TIMEOUT)
        break
      except Exception:
        if i == CHUNK_DOWNLOAD_RETRIES - 1:
          raise
        time.sleep(CHUNK_DOWNLOAD_TIMEOUT)

    resp.raise_for_status()

    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
    return decompressor.decompress(resp.content)


def parse_caibx(caibx_path: str) -> List[Chunk]:
  """Parses the chunks from a caibx file. Can handle both local and remote files.
  Returns a list of chunks with hash, offset and length"""
  if os.path.isfile(caibx_path):
    caibx = open(caibx_path, 'rb')
  else:
    resp = requests.get(caibx_path, timeout=CAIBX_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    caibx = io.BytesIO(resp.content)

  caibx.seek(0, os.SEEK_END)
  caibx_len = caibx.tell()
  caibx.seek(0, os.SEEK_SET)

  # Parse header
  length, magic, flags, min_size, _, max_size = struct.unpack("<QQQQQQ", caibx.read(CA_HEADER_LEN))
  assert flags == flags
  assert length == CA_HEADER_LEN
  assert magic == CA_FORMAT_INDEX

  # Parse table header
  length, magic = struct.unpack("<QQ", caibx.read(CA_TABLE_HEADER_LEN))
  assert magic == CA_FORMAT_TABLE

  # Parse chunks
  num_chunks = (caibx_len - CA_HEADER_LEN - CA_TABLE_MIN_LEN) // CA_TABLE_ENTRY_LEN
  chunks = []

  offset = 0
  for i in range(num_chunks):
    new_offset = struct.unpack("<Q", caibx.read(8))[0]

    sha = caibx.read(32)
    length = new_offset - offset

    assert length <= max_size

    # Last chunk can be smaller
    if i < num_chunks - 1:
      assert length >= min_size

    chunks.append(Chunk(sha, offset, length))
    offset = new_offset

  return chunks


def build_chunk_dict(chunks: List[Chunk]) -> ChunkDict:
  """Turn a list of chunks into a dict for faster lookups based on hash.
  Keep first chunk since it's more likely to be already downloaded."""
  r = {}
  for c in chunks:
    if c.sha not in r:
      r[c.sha] = c
  return r


def extract(target: List[Chunk],
            sources: List[Tuple[str, ChunkReader, ChunkDict]],
            out_path: str,
            progress: Optional[Callable[[int], None]] = None):
  stats: Dict[str, int] = defaultdict(int)

  with open(out_path, 'wb') as out:
    for cur_chunk in target:

      # Find source for desired chunk
      for name, chunk_reader, store_chunks in sources:
        if cur_chunk.sha in store_chunks:
          bts = chunk_reader.read(store_chunks[cur_chunk.sha])

          # Check length
          if len(bts) != cur_chunk.length:
            continue

          # Check hash
          if SHA512.new(bts, truncate="256").digest() != cur_chunk.sha:
            continue

          # Write to output
          out.seek(cur_chunk.offset)
          out.write(bts)

          stats[name] += cur_chunk.length

          if progress is not None:
            progress(sum(stats.values()))

          break
      else:
        raise RuntimeError("Desired chunk not found in provided stores")

  return stats


def print_stats(stats: Dict[str, int]):
  total_bytes = sum(stats.values())
  print(f"Total size: {total_bytes / 1024 / 1024:.2f} MB")
  for name, total in stats.items():
    print(f"  {name}: {total / 1024 / 1024:.2f} MB ({total / total_bytes * 100:.1f}%)")


def extract_simple(caibx_path, out_path, store_path):
  # (name, callback, chunks)
  target = parse_caibx(caibx_path)
  sources = [
    # (store_path, RemoteChunkReader(store_path), build_chunk_dict(target)),
    (store_path, FileChunkReader(store_path), build_chunk_dict(target)),
  ]

  return extract(target, sources, out_path)


if __name__ == "__main__":
  caibx = sys.argv[1]
  out = sys.argv[2]
  store = sys.argv[3]

  stats = extract_simple(caibx, out, store)
  print_stats(stats)
