"""Multipart writes cannot escape through a replaced parent directory."""
from io import BytesIO
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

from app.exceptions import BusinessRuleError
from app.infra import multipart_storage


@pytest.mark.parametrize('operation', ['chunk', 'metadata'])
def test_multipart_write_rejects_directory_link(tmp_path, operation):
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'private.txt').write_bytes(b'previous')
    alias = tmp_path / 'alias'
    alias.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(BusinessRuleError):
        if operation == 'chunk':
            multipart_storage.save_chunk(str(alias), 0, FileStorage(stream=BytesIO(b'abc')), 3, 3)
        else:
            multipart_storage.write_metadata(str(alias / 'meta.json'), {'filename': 'abc'})
    assert sorted(path.name for path in foreign.iterdir()) == ['private.txt']
    assert (foreign / 'private.txt').read_bytes() == b'previous'


@pytest.mark.skipif(not multipart_storage._DESCRIPTORS, reason='POSIX anchored publication')
@pytest.mark.parametrize('adapter', ['http', 'legacy'])
def test_parent_replacement_during_write_publishes_only_in_opened_directory(tmp_path, adapter):
    root = tmp_path / 'parts'
    workspace = root / '1'
    chunks = workspace / 'upload-session' / 'chunks'
    chunks.mkdir(parents=True)
    foreign = tmp_path / 'foreign'
    (foreign / 'upload-session' / 'chunks').mkdir(parents=True)
    private = foreign / 'upload-session' / 'chunks' / '0.part'
    private.write_bytes(b'previous')
    retired = tmp_path / 'retired'

    def replace_parent():
        workspace.rename(retired)
        workspace.symlink_to(foreign, target_is_directory=True)

    class ReplacingStream(BytesIO):
        def read(self, *args):
            if not retired.exists():
                replace_parent()
            return super().read(*args)

    if adapter == 'http':
        upload = FileStorage(stream=ReplacingStream(b'abc'))
    else:
        def save(path):
            replace_parent()
            with open(path, 'wb') as target:
                target.write(b'abc')
        upload = SimpleNamespace(save=save)
    multipart_storage.save_chunk(str(chunks), 0, upload, 3, 3, root=str(root))
    assert (retired / 'upload-session' / 'chunks' / '0.part').read_bytes() == b'abc'
    assert private.read_bytes() == b'previous'
    assert [path.name for path in (retired / 'upload-session' / 'chunks').iterdir()] == ['0.part']
