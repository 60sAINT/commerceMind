"""Skill 文件管理：真实目录、内容版本、失败恢复和重载后的懒加载。"""
import asyncio

import httpx
import pytest
import pytest_asyncio

from test_skill_import_api import SKILL_MD, make_app, package


@pytest_asyncio.fixture
async def client(tmp_path):
    app = make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield client, app


async def seed(client):
    response = await client.post('/skills/import', json={'id': 'example', 'files': package()})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_create_read_edit_delete_flow(client, tmp_path):
    http, app = client
    created = await seed(http)
    pkg = (await http.get('/skills/example/files')).json()
    assert pkg['revision'] == created['revision']
    assert sorted(pkg['files'], key=lambda f: f['path']) == sorted(package(), key=lambda f: f['path'])
    # 热重载必须清空此前已被读取过的正文缓存。
    assert '先确认订单号' in app.state.skill_manager.load_skill('example').body
    files = [{'path': 'SKILL.md', 'content': SKILL_MD.replace('先确认订单号', '先确认交易号')},
             {'path': 'templates/new.txt', 'content': '新模板'}]
    saved = await http.put('/skills/example/files', json={'revision': pkg['revision'], 'files': files})
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result['revision'] != pkg['revision']
    assert result['report']['updated'] == ['example']
    assert not (tmp_path / 'example/references/refund-policy.md').exists()
    assert (tmp_path / 'example/templates/new.txt').read_text() == '新模板'
    assert '先确认交易号' in app.state.skill_manager.load_skill('example').body
    summary = (await http.get('/skills')).json()
    assert summary['management_enabled'] is True
    assert summary['version'] == result['report']['version']
    assert summary['limits']['max_files'] == 64
    deleted = await http.delete('/skills/example', params={'revision': result['revision']})
    assert deleted.status_code == 200
    assert deleted.json()['report']['removed'] == ['example']
    assert not (tmp_path / 'example').exists()
    assert (await http.get('/skills/example/files')).status_code == 404
    assert app.state.skill_manager.get('example') is None


@pytest.mark.asyncio
async def test_stale_revision_cannot_save_or_delete(client, tmp_path):
    http, _ = client
    created = await seed(http)
    path = tmp_path / 'example/SKILL.md'
    path.write_text(SKILL_MD + '\n外部修改', encoding='utf-8')
    for method in ('put', 'delete'):
        if method == 'put':
            response = await http.put('/skills/example/files', json={'revision': created['revision'], 'files': package()})
        else:
            response = await http.delete('/skills/example', params={'revision': created['revision']})
        assert response.status_code == 409
        assert response.json()['error_code'] == 'skill_revision_conflict'
        assert '外部修改' in path.read_text()


@pytest.mark.asyncio
async def test_concurrent_saves_only_one_wins(client):
    http, _ = client
    created = await seed(http)
    responses = await asyncio.gather(*[
        http.put('/skills/example/files', json={'revision': created['revision'], 'files': package(SKILL_MD + f'\n{i}')})
        for i in range(2)
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['save', 'delete'])
async def test_mutation_rollback_after_reload_error(client, tmp_path, monkeypatch, action):
    http, app = client
    created = await seed(http)
    manager = app.state.skill_manager
    real_reload = manager.reload
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('模拟扫描故障')
        return real_reload()

    monkeypatch.setattr(manager, 'reload', flaky)
    if action == 'save':
        response = await http.put('/skills/example/files', json={'revision': created['revision'], 'files': package(SKILL_MD + 'new')})
    else:
        response = await http.delete('/skills/example', params={'revision': created['revision']})
    assert response.status_code == 500
    restored = (await http.get('/skills/example/files')).json()
    assert restored['revision'] == created['revision']
    assert manager.get('example') is not None
    assert not list(tmp_path.glob('.trash-*'))
    assert not list(tmp_path.glob('.staging-*'))


@pytest.mark.asyncio
async def test_invalid_edit_keeps_original(client):
    http, _ = client
    created = await seed(http)
    response = await http.put('/skills/example/files', json={'revision': created['revision'], 'files': package('invalid')})
    assert response.status_code == 422
    assert (await http.get('/skills/example/files')).json()['revision'] == created['revision']


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['symlink', 'binary', 'unknown', 'oversized'])
async def test_read_refuses_unsafe_or_incomplete_edit(client, tmp_path, bad):
    http, _ = client
    created = await seed(http)
    root = tmp_path / 'example'
    if bad == 'symlink':
        (root / 'references/link.txt').symlink_to(root / 'SKILL.md')
    elif bad == 'binary':
        (root / 'references/blob').write_bytes(b'\xff\x00')
    elif bad == 'unknown':
        (root / 'unknown.txt').write_text('不能静默丢弃')
    else:
        (root / 'references/big').write_bytes(b'x' * 65537)
    response = await http.get('/skills/example/files')
    assert response.status_code in (413, 422)
    assert (await http.delete('/skills/example', params={'revision': created['revision']})).status_code in (413, 422)
    assert root.exists()


@pytest.mark.asyncio
async def test_management_routes_hidden_without_debug(tmp_path):
    app = make_app(tmp_path, debug=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as http:
        assert (await http.get('/skills')).json()['management_enabled'] is False
        assert (await http.get('/skills/example/files')).status_code == 404
        assert (await http.put('/skills/example/files', json={})).status_code == 404
        assert (await http.delete('/skills/example')).status_code == 404
        assert (await http.post('/skills/reload')).status_code == 404


@pytest.mark.asyncio
async def test_resource_content_changes_revision_and_reload_clears_cache(client):
    http, app = client
    created = await seed(http)
    manager = app.state.skill_manager
    manager.load_skill('example')
    edited = package(resource='修改资源')
    response = await http.put('/skills/example/files', json={'revision': created['revision'], 'files': edited})
    assert response.status_code == 200
    assert response.json()['revision'] != created['revision']
    assert manager._body_cache == {}
