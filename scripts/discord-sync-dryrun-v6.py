#!/usr/bin/env python3
"""スマート統合モード: 既存Discord構造を尊重しつつMac CCに合わせる"""
import json, sqlite3, requests, unicodedata
from collections import defaultdict

GUILD_ID = '1495139872418042020'
with open('/home/ubuntu/discord-bot/.env') as f:
    for ln in f:
        if ln.startswith('DISCORD_TOKEN='):
            TOKEN = ln.strip().split('=',1)[1].strip('\'\"')
            break
H = {'Authorization': f'Bot {TOKEN}'}
API = 'https://discord.com/api/v10'
def norm(s): return unicodedata.normalize('NFC', s).strip() if s else s

sessions = json.load(open('/tmp/active_sessions.json'))

# Discord現状（カテゴリ・チャンネル・既存スレッド）
chans = requests.get(f'{API}/guilds/{GUILD_ID}/channels', headers=H).json()
cats = {norm(c['name']): c['id'] for c in chans if c['type']==4}

# カテゴリ別のチャンネル（name→id）
chan_by_cat = defaultdict(dict)
for c in chans:
    if c['type']==0 and c.get('parent_id'):
        cat = next((n for n,i in cats.items() if i==c['parent_id']), None)
        if cat: chan_by_cat[cat][norm(c['name'])] = c['id']

# アクティブスレッド取得
threads = requests.get(f'{API}/guilds/{GUILD_ID}/threads/active', headers=H).json().get('threads', [])
thread_by_parent = defaultdict(dict)  # channel_id → {name: thread_id}
for t in threads:
    thread_by_parent[t['parent_id']][norm(t['name'])] = t['id']

# sessions.db
con = sqlite3.connect('/home/ubuntu/discord-bot/sessions.db')
db_rows = list(con.execute('SELECT channel_id, session_id, work_dir, thread_title FROM sessions'))
con.close()
mapped_sids = set(r[1] for r in db_rows if r[1])
# channel_id → session_id マップ
ch_to_sid = {r[0]: r[1] for r in db_rows}

# グループ別セッション
by_group = defaultdict(list)
for s in sessions:
    by_group[s['group']].append(s)

# === プラン構築 ===
plan = {
    'new_cats': set(),
    'link_existing': [],     # (session, channel_or_thread_id, 'channel'/'thread')
    'new_threads': [],        # (session, target_channel_id or None) 
    'new_chans_needed': [],   # カテゴリにチャンネルが0の場合だけ
}

for g, sess_list in by_group.items():
    # カテゴリ
    if g not in cats: plan['new_cats'].add(g)
    
    existing_chans = chan_by_cat.get(g, {})
    chan_names = set(existing_chans.keys())
    
    for s in sess_list:
        title = norm(s.get('title','') or '')
        sid = s.get('cliSessionId') or ''
        if sid.startswith('local_'): sid = sid[6:]
        
        matched = False
        
        # 1) 既存チャンネル名がセッションtitleと一致？
        for cn, cid in existing_chans.items():
            if cn.lower() == title.lower():
                plan['link_existing'].append((s, cid, 'channel', cn))
                matched = True
                break
        
        # 2) 既存スレッド名がtitleと一致？（全チャンネル横断）
        if not matched:
            for pid, tmap in thread_by_parent.items():
                if title.lower() in {k.lower() for k in tmap}:
                    plan['link_existing'].append((s, tmap[title], 'thread', title))
                    matched = True
                    break
        
        # 3) sessions.dbでsession_idが既にマップ済み？
        if not matched and sid in mapped_sids:
            plan['link_existing'].append((s, None, 'db-mapped', sid[:8]))
            matched = True
        
        if not matched:
            plan['new_threads'].append(s)

# 新規スレッドを置く先のチャンネルを決める
# - 該当カテゴリにチャンネルが既存 → 最初のチャンネル使う
# - 無ければ new_chans_needed に追加
for s in plan['new_threads']:
    g = s['group']
    existing = chan_by_cat.get(g, {})
    if not existing and g not in [x[0] for x in plan['new_chans_needed']]:
        plan['new_chans_needed'].append((g, g))  # (カテゴリ, チャンネル名)

# === 出力 ===
print('='*60)
print(f'スマート統合モード ドライラン')
print('='*60)
print(f'対象セッション: {len(sessions)}件')
print()
print(f'■ 新規カテゴリ: {len(plan["new_cats"])}件')
for c in sorted(plan['new_cats']): print(f'    + {c}')
print()
print(f'■ 新規チャンネル（既存チャンネル0個のカテゴリだけ）: {len(plan["new_chans_needed"])}件')
for g, n in plan['new_chans_needed']: print(f'    + [{g}] {n}')
print()
print(f'■ 既存Discord構造と自動リンク: {len(plan["link_existing"])}件')
for s, eid, typ, name in plan['link_existing']:
    print(f'    🔗 [{s["group"]}] {s.get("title","")[:40]} → {typ}({name})')
print()
print(f'■ 新規スレッド作成: {len(plan["new_threads"])}件')
for s in plan['new_threads']:
    print(f'    + [{s["group"]}] {s.get("title","")[:50]}')
print()
print('='*60)
print(f'合計: カテゴリ{len(plan["new_cats"])} + チャンネル{len(plan["new_chans_needed"])} + スレッド{len(plan["new_threads"])} + リンク{len(plan["link_existing"])}')
print('='*60)
