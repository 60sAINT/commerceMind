"""单入口上传：SKILL.md / 单根目录 ZIP，预览不发布，确认后安装重载。"""
import io
import stat
import zipfile

import httpx
import pytest

from test_skill_import_api import make_app

MD = '---\nname: my-skill\ndescription: 上传测试技能\n---\n\n# 步骤\n\n先检查订单。\n'


def archive(entries):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in entries:
            z.writestr(name, content)
    return raw.getvalue()


async def upload(app, raw, filename='my-skill.zip', preview=False):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        return await client.post('/skills/upload' + ('/preview' if preview else ''),
                                 params={'filename': filename}, content=raw,
                                 headers={'Content-Type': 'application/octet-stream'})


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['md', 'zip'])
async def test_preview_then_upload(tmp_path, kind):
    app = make_app(tmp_path)
    raw = MD.encode() if kind == 'md' else archive([
        ('my-skill/', ''), ('my-skill/SKILL.md', MD),
        ('my-skill/references/a.md', '参考资料'), ('my-skill/templates/', ''),
        ('__MACOSX/my-skill/._SKILL.md', 'Finder metadata'),
    ])
    filename = 'SKILL.md' if kind == 'md' else 'my-skill.zip'
    initial = app.state.skill_manager.catalog.version
    response = await upload(app, raw, filename, preview=True)
    assert response.status_code == 200, response.text
    assert response.json()['skill']['id'] == 'my-skill'
    assert response.json()['file_count'] == (1 if kind == 'md' else 2)
    assert not (tmp_path / 'my-skill').exists()
    assert app.state.skill_manager.catalog.version == initial
    response = await upload(app, raw, filename)
    assert response.status_code == 200, response.text
    assert response.json()['report']['added'] == ['my-skill']
    assert (tmp_path / 'my-skill/SKILL.md').read_text() == MD
    assert app.state.skill_manager.catalog.version == initial + 1
    assert (await upload(app, raw, filename, preview=True)).status_code == 409
    assert (await upload(app, raw, filename)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('entries,code', [
    ([('SKILL.md', MD)], 'skill_zip_structure_invalid'),
    ([('my-skill/SKILL.md', MD), ('another/SKILL.md', MD)], 'skill_zip_structure_invalid'),
    ([('other-skill/SKILL.md', MD)], 'skill_name_mismatch'),
    ([('my-skill/references/a.md', 'no manifest')], 'skill_manifest_missing'),
    ([('my-skill/SKILL.md', MD), ('my-skill/references/../../escape', 'x')], 'skill_path_invalid'),
    ([('/absolute/SKILL.md', MD)], 'skill_path_invalid'),
    ([('my-skill/SKILL.md', MD), ('my-skill/references\\a.md', 'x')], 'skill_path_invalid'),
    ([('my-skill/SKILL.md', MD), ('my-skill/SKILL.md', MD)], 'skill_path_invalid'),
    ([('my-skill/SKILL.md', MD), ('my-skill/references/binary', b'\xff\x00')], 'skill_content_not_text'),
    ([('my-skill/SKILL.md', MD), ('my-skill/unknown/', '')], 'skill_path_invalid'),
])
async def test_invalid_zip_does_not_install(tmp_path, entries, code):
    app = make_app(tmp_path)
    response = await upload(app, archive(entries))
    assert response.status_code == 422, response.text
    assert response.json()['error_code'] == code
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_zip_rejects_symlinks(tmp_path):
    app = make_app(tmp_path)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as z:
        z.writestr('my-skill/SKILL.md', MD)
        link = zipfile.ZipInfo('my-skill/references/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, '/etc/passwd')
    response = await upload(app, raw.getvalue())
    assert response.status_code == 422
    assert response.json()['error_code'] == 'skill_path_invalid'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw,filename,code', [
    (b'broken archive', 'my-skill.zip', 'skill_zip_invalid'),
    (b'', 'SKILL.md', 'skill_files_empty'),
    (MD.encode(), 'arbitrary.md', 'skill_upload_type_invalid'),
    (MD.replace('my-skill', 'Chinese_name').encode(), 'SKILL.md', 'skill_name_invalid'),
    (b'---\nname: my-skill\n', 'SKILL.md', 'skill_front_matter_invalid'),
])
async def test_invalid_input(tmp_path, raw, filename, code):
    response = await upload(make_app(tmp_path), raw, filename)
    assert response.status_code == 422
    assert response.json()['error_code'] == code


@pytest.mark.asyncio
async def test_archive_expanded_size_and_count_limits(tmp_path):
    app = make_app(tmp_path, upload_max_file_bytes=200)
    raw = archive([('my-skill/SKILL.md', MD), ('my-skill/references/big', 'x' * 201)])
    assert (await upload(app, raw)).status_code == 413
    app = make_app(tmp_path, upload_max_files=1)
    raw = archive([('my-skill/SKILL.md', MD), ('my-skill/references/a', 'x')])
    assert (await upload(app, raw)).status_code == 413
    app = make_app(tmp_path, upload_max_total_bytes=20)
    assert (await upload(app, b'x' * 21, 'SKILL.md')).status_code == 413
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_upload_hidden_without_debug(tmp_path):
    app = make_app(tmp_path, debug=False)
    assert (await upload(app, MD.encode(), 'SKILL.md')).status_code == 404
    assert (await upload(app, MD.encode(), 'SKILL.md', preview=True)).status_code == 404
