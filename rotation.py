"""Exact-text template validation and selection. No I/O or implicit retries."""
import hashlib
import json
import secrets


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def text_revision(text):
    if not isinstance(text, str) or not text.strip() or '\0' in text:
        raise ValueError('Message/template must be nonblank text without NUL')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def catalog(config):
    if 'rotation' not in config:
        text_revision(config.get('message'))
        return None
    if 'message' in config:
        raise ValueError('Configure message OR rotation, not both')
    campaign = config.get('campaign_id')
    raw = config['rotation']
    if not isinstance(campaign, str) or not campaign.strip() or not isinstance(raw, dict):
        raise ValueError('Rotation requires campaign_id and a rotation object')
    mode = raw.get('mode', 'round_robin')
    if mode not in ('round_robin', 'random_no_immediate_repeat'):
        raise ValueError('Invalid rotation mode')
    templates = raw.get('templates')
    if not isinstance(templates, list) or not templates:
        raise ValueError('Rotation requires at least one template')
    result, ids, texts = [], set(), set()
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError('Template must be an object')
        tid, text = item.get('id'), item.get('text')
        if not isinstance(tid, str) or not tid.strip() or tid in ids:
            raise ValueError('Template IDs must be distinct nonempty strings')
        revision = text_revision(text)
        if text in texts:
            raise ValueError('Duplicate template content is not allowed')
        ids.add(tid)
        texts.add(text)
        result.append(dict(id=tid, text=text, revision=revision))
    return dict(campaign_id=campaign, mode=mode, templates=result)


def choose(cat, last=None):
    templates = cat['templates']
    if not last or len(templates) == 1:
        return templates[0] if cat['mode'] == 'round_robin' else secrets.choice(templates)
    if cat['mode'] == 'round_robin':
        ids = [t['id'] for t in templates]
        return templates[(ids.index(last['id']) + 1) % len(ids)] if last['id'] in ids else templates[0]
    candidates = [t for t in templates if t['id'] != last['id'] and t['text'] != last['text']]
    if not candidates:
        raise ValueError('No non-repeating template available')
    return secrets.choice(candidates)
