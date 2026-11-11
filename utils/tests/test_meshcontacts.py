"""Address-book persistence, routing and real MeshChat keyboard flows."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'modules/scripts/lib'))
sys.path.insert(0, str(ROOT / 'utils'))
import meshcontacts as C
import meshback
import meshui
import mesh_preview

T0 = 1800000000


def row(**kw):
    r = dict(protocol='meshtastic', nid='!1234abcd', name='Trail friend',
             email='', note='')
    r.update(kw)
    return r


def test_round_trip_and_protocol_scoping(tmp_path):
    p = str(tmp_path / 'contacts.json')
    s = C.Store(p)
    a = s.put(row(nid='1234ABCD', email='friend@example.com', note='Meet by the trail'))
    assert a['nid'] == '!1234abcd'
    s.put(row(protocol='demo', name='Demo friend'))
    loaded = C.Store(p)
    assert loaded.error is None and loaded.rows == s.rows
    assert loaded.find('meshtastic', '!1234abcd')['note'] == 'Meet by the trail'
    assert loaded.find('demo', '!1234abcd')['name'] == 'Demo friend'
    assert loaded.find('raw', '!1234abcd') is None
    assert not Path(p + '.tmp').exists()


@pytest.mark.parametrize('change', [dict(name=''), dict(nid=''), dict(nid='!ffffffff'),
    dict(nid='!00000000'), dict(nid='alice@example.com'), dict(note='a' * 241),
    dict(email=1), dict(name='Bad\x1b[2J'), dict(email='a\nb'), dict(protocol='unknown'), dict(nid='!abcdefg0')])
def test_invalid_contacts_rejected(tmp_path, change):
    s = C.Store(str(tmp_path / 'contacts.json'))
    with pytest.raises(ValueError):
        s.put(row(**change))
    assert s.rows == [] and not Path(s.path).exists()


def test_duplicate_update_and_delete(tmp_path):
    s = C.Store(str(tmp_path / 'contacts.json'))
    s.put(row())
    with pytest.raises(ValueError, match='already'):
        s.put(row(name='Other person'))
    key = ('meshtastic', '!1234abcd')
    s.put(row(name='Renamed', note='Updated'), key)
    assert len(s.rows) == 1 and C.Store(s.path).rows[0]['name'] == 'Renamed'
    s.delete(key)
    assert C.Store(s.path).rows == []


def test_failed_replace_preserves_saved_book_and_memory(tmp_path, monkeypatch):
    s = C.Store(str(tmp_path / 'contacts.json'))
    s.put(row())
    before = Path(s.path).read_bytes()
    def fail(*args):
        raise OSError(28, 'No space')
    monkeypatch.setattr(C.os, 'rename', fail)
    with pytest.raises(OSError):
        s.put(row(nid='!87654321', name='Second'))
    assert Path(s.path).read_bytes() == before
    assert len(s.rows) == 1 and not Path(s.path + '.tmp').exists()


@pytest.mark.parametrize('content', ['{bad', '[]', '{"version":2,"contacts":[]}',
    '{"version":1,"contacts":[{}]}'])
def test_damaged_book_not_overwritten(tmp_path, content):
    p = tmp_path / 'contacts.json'
    p.write_text(content)
    s = C.Store(str(p))
    assert s.error
    with pytest.raises(ValueError):
        s.put(row())
    assert p.read_text() == content


@pytest.fixture
def app(tmp_path):
    return meshui.App(meshback.SimBackend(clock=lambda: T0), T0,
                      contacts_path=str(tmp_path / 'contacts.json'))


def command(app, text):
    for c in text:
        app.key(c, T0)
    app.key('\r', T0)


def field(app, index, text):
    app.contact_sel = index
    app.key('\r', T0)
    for c in text:
        app.key(c, T0)
    app.key('\r', T0)


def action(app, name):
    app.contact_sel = len(C.FIELDS) + C.ACTIONS.index(name)
    app.key('\r', T0)


def test_add_from_node_edit_reload_message_no_personal_data_transmitted(app, monkeypatch):
    command(app, '/nodes')
    node = app.nodes[app.sel]
    app.key('c', T0)
    assert app.mode == 'contact' and app.contact_edit['nid'] == node.nid
    field(app, 0, 'My radio friend')
    field(app, 2, 'friend@example.com')
    field(app, 3, 'Met at the campsite')
    action(app, 'Save changes')
    assert app.contact_original['name'] == 'My radio friend'
    app.key('ESC', T0)
    assert app.mode == 'contacts' and app.dialog is None
    app.key('m', T0)
    assert app.cur == 'dm:' + node.nid and app.conv().title == 'My radio friend'
    sent = []
    monkeypatch.setattr(app.backend, 'send', lambda key, text: sent.append((key, text)) or 1)
    command(app, 'Hello')
    assert sent == [('dm:' + node.nid, 'Hello')]
    restored = meshui.App(meshback.SimBackend(clock=lambda: T0), T0,
                          contacts_path=app.address_book.path)
    restored.backend._nodes = []
    restored._refresh(T0)
    command(restored, '/dm My radio friend')
    assert restored.mode == 'chat' and restored.cur == 'dm:' + node.nid
    command(restored, '/contact')
    assert restored.contact_edit['email'] == 'friend@example.com'
    assert restored.contact_edit['note'] == 'Met at the campsite'
    assert restored.contact_edit == restored.contact_original


def test_manual_contact_validation_cancel_and_delete(app):
    command(app, '/contacts')
    app.key('a', T0)
    field(app, 0, 'Manual friend')
    field(app, 1, '!1234abcd')
    action(app, 'Save changes')
    field(app, 3, 'Unsaved note')
    app.key('ESC', T0)
    assert app.dialog['title'] == 'Unsaved contact'
    app.key('DOWN', T0)
    app.key('\r', T0)
    assert app.mode == 'contacts'
    app.key('\r', T0)
    assert app.contact_edit['note'] == ''
    action(app, 'Delete contact')
    app.key('ESC', T0)
    assert len(app.address_book.rows) == 1
    action(app, 'Delete contact')
    app.key('\r', T0)
    assert app.mode == 'contacts' and not app.address_book.rows


def test_blank_name_and_invalid_node_block_save_and_message(app):
    app.contact_open()
    action(app, 'Message')
    assert app.mode == 'contact' and not app.address_book.rows
    field(app, 0, 'Person')
    field(app, 1, 'bad ID')
    action(app, 'Save changes')
    assert 'hex' in app.notice[0] and not app.address_book.rows


@pytest.mark.parametrize('cols,rows', [(32, 12), (40, 16), (53, 19), (64, 29)])
def test_lists_forms_full_notes_and_scrolling_menus_fit(app, cols, rows):
    for i in range(20):
        app.address_book.put(row(protocol='demo', nid='!%08x' % (i + 1),
                                 name='Person %02d' % i, note='A longer note. ' * 16))
    app.contacts_open(T0)
    app.sel = 19
    mesh_preview.emulate(app.render(cols, rows, T0), cols, rows)
    app.key('\r', T0)
    for index in (0, 7):
        app.contact_sel = index
        mesh_preview.emulate(app.render(cols, rows, T0), cols, rows)
    action(app, 'View details')
    for _ in range(20):
        app.key('DOWN', T0)
        mesh_preview.emulate(app.render(cols, rows, T0), cols, rows)
    app.key('ESC', T0)
    assert app.mode == 'contact'
    app.mode = 'menu'
    for i in range(len(app.menu_items())):
        app.sel = i
        mesh_preview.emulate(app.render(cols, rows, T0), cols, rows)


def test_contacts_in_encrypted_settings_backup(tmp_path, monkeypatch):
    import backup
    monkeypatch.setattr(backup, 'ITERATIONS', 5)
    root = tmp_path / 'flash'
    root.mkdir()
    s = C.Store(str(root / '.meshcontacts.json'))
    s.put(row(email='friend@example.com'))
    payload = backup.collect(str(root))
    payload = backup.unseal(backup.seal(payload, 'test passphrase'), 'test passphrase')
    # Exercise the same whitelist used by settings import.
    assert '.meshcontacts.json' in backup.FILES
    restored = tmp_path / 'restored'
    restored.mkdir()
    backup.apply(payload, str(restored))
    assert C.Store(str(restored / '.meshcontacts.json')).rows == s.rows


def test_contact_names_label_incoming_messages_without_changing_sender_id(app):
    app.address_book.put(row(protocol='demo', nid='!0000b0b1', name='My friend'))
    app._on_msg(('msg', 'dm:!0000b0b1', '!0000b0b1', 'Bob', 'Hi', T0))
    c = app.convs['dm:!0000b0b1']
    assert c.title == 'My friend' and c.msgs[-1].name == 'My friend'
    assert c.msgs[-1].sid == '!0000b0b1'


def test_same_name_contacts_require_selection_and_modes_do_not_mix(app):
    app.address_book.put(row(protocol='demo', nid='!00000001', name='Friend'))
    app.address_book.put(row(protocol='demo', nid='!00000002', name='Friend'))
    app.address_book.put(row(protocol='meshtastic', name='Real radio'))
    command(app, '/dm Friend')
    assert 'Several' in app.notice[0] and app.cur == 'ch:0'
    assert len(app.contact_rows()) == 2
