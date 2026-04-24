#!/usr/bin/env python3
"""Phase 1: Mac CC → Discord スマート統合実行スクリプト"""
import json, sqlite3, requests, unicodedata, time, sys
from collections import defaultdict

DRY_RUN = '--dry-run' in sys.argv

GUILD_ID = '1495139872418042020'
with open('/home/ubuntu/discord-bot/.env') as f:
    for ln in f:
        if ln.startswith('DISCORD_TOKEN='):
            TOKEN = ln.strip().split('=',1)[1].strip('\'"')
            break
H = {'Authorization': f'Bot {TOKEN}', 'Content-Type': 'application/json'}
API = 'https://discord.com/api/v10'
def norm(s): return unicodedata.normalize('NFC', s).strip() if s else s

def api_post(path, data, dry=False):
    if dry:
        print(f'    [DRY] POST {path} {json.dumps(data, ensure_ascii=False)[:80]}')
        return {'id': 'DRY_' + str(int(time.time()*1000))}
    r = requests.post(f'{API}{path}', headers=H, json=data)
    if r.status_code not in (200, 201):
        print(f'    ERROR {r.status_code}: {r.text[:200]}')
        return None
    time.sleep(0.6)  # rate limit
    return r.json()

def db_upsert(con, channel_id, session_id, work_dir, thread_title):
    con.execute('''INSERT OR REPLACE INTO sessions (channel_id, session_id, work_dir, thread_title)
                   VALUES (?,?,?,?)''', (channel_id, session_id, work_dir, thread_title))
    con.commit()

sessions = json.load(open('/tmp/active_sessions.json'))

# Discord現状
chans = requests.get(f'{API}/guilds/{GUILD_ID}/channels', headers=H).json()
cats = {norm(c['name']): c['id'] for c in chans if c['type']==4}
chan_by_cat = defaultdict(dict)
for c in chans:
    if c['type']==0 and c.get('parent_id'):
        cat = next((n for n,i in cats.items() if i==c['parent_id']), None)
        if cat: chan_by_cat[cat][norm(c['name'])] = c['id']

threads_resp = requests.get(f'{API}/guilds/{GUILD_ID}/threads/active', headers=H).json()
active_threads = threads_resp.get('threads', [])
thread_by_parent = defaultdict(dict)
for t in active_threads:
    thread_by_parent[t['parent_id']][norm(t['name'])] = t['id']

con = sqlite3.connect('/home/ubuntu/discord-bot/sessions.db')
db_rows = list(con.execute('SELECT channel_id, session_id, work_dir, thread_title FROM sessions'))
mapped_sids = set(r[1] for r in db_rows if r[1])

by_group = defaultdict(list)
for s in sessions:
    by_group[s['group']].append(s)

plan = {'new_cats': set(), 'link_existing': [], 'new_threads': [], 'new_chans_needed': []}

for g, sess_list in by_group.items():
    if g not in cats: plan['new_cats'].add(g)
    existing_chans = chan_by_cat.get(g, {})
    for s in sess_list:
        title = norm(s.get('title','') or '')
        sid = s.get('cliSessionId') or ''
        if sid.startswith('local_'): sid = sid[6:]
        matched = False
        for cn, cid in existing_chans.items():
            if cn.lower() == title.lower():
                plan['link_existing'].append((s, cid, 'channel', cn)); matched = True; break
        if not matched:
            for pid, tmap in thread_by_parent.items():
                if title.lower() in {k.lower() for k in tmap}:
                    tname = next((k for k in tmap if k.lower() == title.lower()), None)
                    tid = tmap[tname] if tname else list(tmap.values())[0]
                    plan['link_existing'].append((s, tid, 'thread', title))
                    matched = True; break
        if not matched and sid in mapped_sids:
            plan['link_existing'].append((s, None, 'db-mapped', sid[:8])); matched = True
        if not matched:
            plan['new_threads'].append(s)

for s in plan['new_threads']:
    g = s['group']
    existing = chan_by_cat.get(g, {})
    if not existing and g not in [x[0] for x in plan['new_chans_needed']]:
        plan['new_chans_needed'].append((g, g))

print('='*60)
print(f'{"[DRY RUN] " if DRY_RUN else ""}Phase 1 実行プラン')
print('='*60)
print(f'新規カテゴリ: {sorted(plan["new_cats"])}')
print(f'新規チャンネル: {len(plan["new_chans_needed"])}件')
print(f'新規スレッド: {len(plan["new_threads"])}件')
print(f'既存リンク: {len(plan["link_existing"])}件')
print()

# === STEP 1: カテゴリ作成 ===
print('--- STEP 1: カテゴリ作成 ---')
for cat_name in sorted(plan['new_cats']):
    print(f'  + カテゴリ: {cat_name}')
    res = api_post(f'/guilds/{GUILD_ID}/channels', {'name': cat_name, 'type': 4}, DRY_RUN)
    if res and not DRY_RUN:
        cats[cat_name] = res['id']
        print(f'    → ID: {res["id"]}')

# === STEP 2: チャンネル作成 ===
print('--- STEP 2: チャンネル作成 ---')
for g, chan_name in plan['new_chans_needed']:
    cat_id = cats.get(g)
    if not cat_id:
        print(f'  !! カテゴリID不明: {g}')
        continue
    print(f'  + チャンネル: [{g}] #{chan_name}')
    res = api_post(f'/guilds/{GUILD_ID}/channels',
                   {'name': chan_name, 'type': 0, 'parent_id': cat_id}, DRY_RUN)
    if res and not DRY_RUN:
        chan_by_cat[g][chan_name] = res['id']
        print(f'    → ID: {res["id"]}')

# === STEP 3: スレッド作成 ===
print('--- STEP 3: スレッド作成 ---')
for s in plan['new_threads']:
    g = s['group']
    title = norm(s.get('title','') or s.get('work_dir','unknown'))[:100]
    sid = s.get('cliSessionId') or ''
    if sid.startswith('local_'): sid = sid[6:]
    work_dir = s.get('work_dir','')

    existing_chans = chan_by_cat.get(g, {})
    if existing_chans:
        target_chan_id = list(existing_chans.values())[0]
        target_chan_name = list(existing_chans.keys())[0]
    else:
        print(f'  !! チャンネルなし: {g}')
        continue

    print(f'  + スレッド: [{g}] #{target_chan_name} → {title[:50]}')
    res = api_post(f'/channels/{target_chan_id}/threads',
                   {'name': title, 'type': 11, 'auto_archive_duration': 10080}, DRY_RUN)
    if res and not DRY_RUN:
        thread_id = res['id']
        db_upsert(con, thread_id, sid, work_dir, title)
        print(f'    → Thread ID: {thread_id}')
    elif DRY_RUN:
        print(f'    sid={sid[:12]}... cwd={work_dir[-40:]}')

# === STEP 4: 既存リンクをDB登録 ===
print('--- STEP 4: 既存リンクをDB登録 ---')
for s, eid, typ, name in plan['link_existing']:
    sid = s.get('cliSessionId') or ''
    if sid.startswith('local_'): sid = sid[6:]
    work_dir = s.get('work_dir','')
    title = norm(s.get('title','') or '')
    if typ == 'db-mapped':
        print(f'  ✓ DB済み: {name}')
        continue
    if not eid:
        print(f'  !! IDなし: {name}')
        continue
    print(f'  🔗 [{typ}] {name[:40]} → {sid[:12]}...')
    if not DRY_RUN:
        db_upsert(con, eid, sid, work_dir, title)

con.close()
print()
print('='*60)
print(f'{"[DRY RUN] " if DRY_RUN else ""}完了')
print('='*60)
