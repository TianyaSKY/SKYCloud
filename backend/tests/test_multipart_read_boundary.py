"""Stored multipart bytes cannot be substituted with filesystem aliases."""
import os
from contextlib import contextmanager

import pytest

from app.exceptions import BusinessRuleError
from app.infra import multipart_storage


def test_chunk_link_is_not_reported_or_assembled(tmp_path):
    chunks = tmp_path / 'chunks'
    chunks.mkdir()
    private = tmp_path / 'private.txt'
    private.write_bytes(b'secret')
    (chunks / '0.part').symlink_to(private)
    destination = tmp_path / 'assembled'
    destination.write_bytes(b'previous')
    assert multipart_storage.list_chunks(str(chunks)) == []
    with pytest.raises(BusinessRuleError):
        multipart_storage.assemble(str(chunks), str(destination), total_chunks=1,
                                   chunk_size=6, total_size=6)
    assert destination.read_bytes() == b'previous'
    assert private.read_bytes() == b'secret'


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX named pipe')
def test_non_regular_chunk_does_not_count_or_block_assembly(tmp_path):
    chunks = tmp_path / 'chunks'
    chunks.mkdir()
    os.mkfifo(chunks / '0.part')
    assert multipart_storage.list_chunks(str(chunks)) == []
    with pytest.raises(BusinessRuleError):
        multipart_storage.assemble(str(chunks), str(tmp_path / 'assembled'),
                                   total_chunks=1, chunk_size=3, total_size=3)
    assert not (tmp_path / 'assembled').exists()


@pytest.mark.skipif(not multipart_storage._DESCRIPTORS, reason='POSIX anchored directory reads')
def test_directory_replacement_keeps_assembly_on_opened_directory(tmp_path, monkeypatch):
    root = tmp_path / 'parts'
    chunks = root / '1' / 'upload-session' / 'chunks'
    chunks.mkdir(parents=True)
    (chunks / '0.part').write_bytes(b'safe!!')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / '0.part').write_bytes(b'secret')
    original_source = multipart_storage._chunk_source

    @contextmanager
    def replace_directory(handle, name):
        chunks.rename(chunks.with_name('retired'))
        chunks.symlink_to(foreign, target_is_directory=True)
        with original_source(handle, name) as source:
            yield source

    monkeypatch.setattr(multipart_storage, '_chunk_source', replace_directory)
    destination = tmp_path / 'assembled'
    multipart_storage.assemble(str(chunks), str(destination), total_chunks=1,
                               chunk_size=6, total_size=6, root=str(root))
    assert destination.read_bytes() == b'safe!!'
    assert (foreign / '0.part').read_bytes() == b'secret'
