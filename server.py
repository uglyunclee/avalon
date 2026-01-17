import socketio
import random
import uuid
import os
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app_asgi = socketio.ASGIApp(sio, app)
app.mount("/", StaticFiles(directory=".", html=True), name="static")

# 標準人數配置 (好人數量, 壞人數量)
BALANCE_CONFIG = {
    1: (1, 0), # 測試用
    5: (3, 2), 6: (4, 2), 7: (4, 3),
    8: (5, 3), 9: (6, 3), 10: (6, 4)
}

QUEST_CONFIG = {
    1: [1, 1, 1, 1, 1], # 測試用
    5: [2, 3, 2, 3, 3], 6: [2, 3, 4, 3, 4], 7: [2, 3, 3, 4, 4], 
    8: [3, 4, 4, 5, 5], 9: [3, 4, 4, 5, 5], 10: [3, 4, 4, 5, 5],
}

rooms = {}

class GameState:
    LOBBY = 'LOBBY'
    TEAM_SELECTION = 'TEAM_SELECTION'
    TEAM_VOTING = 'TEAM_VOTING'
    MISSION = 'MISSION'
    ASSASSINATION = 'ASSASSINATION'
    GAME_OVER = 'GAME_OVER'

async def add_log(room_id, message, color='white'):
    if room_id not in rooms: return
    timestamp = datetime.now().strftime("%H:%M:%S")
    rooms[room_id]['logs'].append({'time': timestamp, 'msg': message, 'color': color})
    await sio.emit('new_log', {'time': timestamp, 'msg': message, 'color': color}, room=room_id)

async def play_sound(room_id, sound_name):
    await sio.emit('play_sound', {'name': sound_name}, room=room_id)

async def broadcast_state(room_id):
    room = rooms[room_id]
    players_list = []
    sorted_tokens = sorted(room['players'].keys(), key=lambda t: room['players'][t]['join_time'])

    for idx, token in enumerate(sorted_tokens):
        p = room['players'][token]
        has_voted = False
        if room['state'] == GameState.TEAM_VOTING: has_voted = token in room['votes']
        elif room['state'] == GameState.MISSION: has_voted = token in room['mission_votes_who']

        players_list.append({
            'token': token, 'name': p['name'], 'avatar': p['avatar'],
            'is_leader': idx == room['leader_index'],
            'in_team': token in room['current_team'],
            'has_voted': has_voted, 'is_connected': p['connected'],
            'has_reset_voted': token in room['reset_votes'],
            'is_ready': p.get('is_ready', False)
        })
        
    required = 0
    try: required = QUEST_CONFIG[len(players_list)][room['quest_index']]
    except: pass

    data = {
        'state': room['state'], 'players': players_list,
        'quest_results': room['quest_results'], 'quest_idx': room['quest_index'],
        'team_size_needed': required, 'vote_track': room['vote_track'],
        'logs': room['logs'],
        'settings': room['settings'] # [新增] 廣播當前設定
    }
    await sio.emit('update_state', data, room=room_id)

@sio.event
async def join_room(sid, data):
    name = data['name']; room_id = data['room_id']; avatar = data['avatar']; token = data.get('token')
    if room_id not in rooms:
        rooms[room_id] = {
            'players': {}, 'sid_map': {}, 'state': GameState.LOBBY,
            'quest_results': [None]*5, 'quest_index': 0, 'leader_index': 0,
            'current_team': [], 'votes': {}, 'mission_votes': [], 'mission_votes_who': [],
            'vote_track': 0, 'logs': [], 'reset_votes': set(),
            # [新增] 遊戲設定預設值
            'settings': {
                'merlin': True, 'percival': True, 
                'assassin': True, 'morgana': True, 
                'mordred': False, 'oberon': False
            }
        }
    room = rooms[room_id]
    
    if token and token in room['players']: # 重連
        p = room['players'][token]; p['sid'] = sid; p['connected'] = True; room['sid_map'][sid] = token; p['name'] = name; p['avatar'] = avatar
        await sio.enter_room(sid, room_id)
        await sio.emit('join_success', {'token': token}, to=sid)
        await add_log(room_id, f"⚡ {name} 重連", "#888")
        if p['role']: await send_role_info(sid, p, list(room['players'].values()))
    else: # 新加入
        new_token = str(uuid.uuid4())
        room['players'][new_token] = {'token': new_token, 'name': name, 'avatar': avatar, 'sid': sid, 'role': None, 'join_time': datetime.now().timestamp(), 'connected': True, 'is_ready': False}
        room['sid_map'][sid] = new_token
        await sio.enter_room(sid, room_id)
        await sio.emit('join_success', {'token': new_token}, to=sid)
        await add_log(room_id, f"👋 {name} 加入", "#aaa")
    await broadcast_state(room_id)

# [新增] 更新設定
@sio.event
async def update_settings(sid, data):
    room_id = data['room_id']; new_settings = data['settings']; room = rooms.get(room_id)
    if not room or room['state'] != GameState.LOBBY: return
    
    # 權限檢查：只有房主(第1個玩家)能改
    sorted_tokens = sorted(room['players'].keys(), key=lambda t: room['players'][t]['join_time'])
    user_token = room['sid_map'].get(sid)
    if user_token != sorted_tokens[0]: return 

    room['settings'] = new_settings
    await broadcast_state(room_id)

@sio.event
async def toggle_ready(sid, room_id):
    if room_id not in rooms: return
    room = rooms[room_id]
    if room['state'] != GameState.LOBBY: return
    token = room['sid_map'].get(sid)
    if not token: return
    room['players'][token]['is_ready'] = not room['players'][token].get('is_ready', False)
    all_ready = all(pl['is_ready'] for pl in room['players'].values())
    
    # 測試用 >=1，正式 >=5
    if all_ready and len(room['players']) >= 1: 
        await start_game_logic(room_id)
    else: 
        await broadcast_state(room_id)

async def start_game_logic(room_id):
    room = rooms[room_id]
    sorted_tokens = sorted(room['players'].keys(), key=lambda t: room['players'][t]['join_time'])
    players_objs = [room['players'][t] for t in sorted_tokens]
    cnt = len(players_objs)
    
    # === 動態身分生成邏輯 ===
    settings = room['settings']
    # 1. 取得該人數的標準配置 (好人數, 壞人數)
    target_good, target_evil = BALANCE_CONFIG.get(cnt, (1, 0)) # 預設fallback
    
    final_roles = []
    
    # 2. 加入必選/勾選的角色
    # 好人陣營
    if settings['merlin']: final_roles.append("梅林")
    if settings['percival']: final_roles.append("派西維爾")
    
    # 壞人陣營
    if settings['assassin']: final_roles.append("刺客")
    if settings['morgana']: final_roles.append("莫甘娜")
    if settings['mordred']: final_roles.append("莫德雷德")
    if settings['oberon']: final_roles.append("奧伯倫")
    
    # 3. 計算剩餘空位
    current_good = len([r for r in final_roles if r in ["梅林", "派西維爾"]])
    current_evil = len([r for r in final_roles if r in ["刺客", "莫甘娜", "莫德雷德", "奧伯倫"]])
    
    needed_good = target_good - current_good
    needed_evil = target_evil - current_evil
    
    # 防呆：如果勾選太多特殊角色超過上限，就隨機移除(或是這裡簡單處理：直接不管，讓它變成超多壞人局，玩家自己負責)
    # 這裡採用：補滿忠臣和爪牙。如果已經超過，則不補。
    for _ in range(max(0, needed_good)): final_roles.append("忠臣")
    for _ in range(max(0, needed_evil)): final_roles.append("壞人") # 爪牙
    
    # 如果人數還是不對(例如勾太多)，截斷或補滿
    if len(final_roles) > cnt:
        final_roles = final_roles[:cnt] # 截斷
    while len(final_roles) < cnt:
        final_roles.append("忠臣") # 不夠就補好人
        
    random.shuffle(final_roles)
    
    room['reset_votes'] = set()
    room['state'] = GameState.TEAM_SELECTION
    room['quest_index'] = 0
    room['leader_index'] = 0
    room['quest_results'] = [None] * 5
    room['vote_track'] = 0
    room['logs'] = []

    for i, p_obj in enumerate(players_objs): p_obj['role'] = final_roles[i]
    for p_obj in players_objs: await send_role_info(p_obj['sid'], p_obj, players_objs)
    
    # 產生簡介文字
    role_summary = ", ".join(set(final_roles)) # 去重顯示
    await add_log(room_id, f"🎲 本局板子: {role_summary}", "cyan")
    await add_log(room_id, "🎮 遊戲開始！", "gold")
    await broadcast_state(room_id)

async def send_role_info(sid, p_obj, all_players):
    my_role = p_obj['role']
    info = {'role': my_role, 'teammates': []}
    evil_team_names = [p['name'] for p in all_players if p['role'] in ["莫甘娜", "刺客", "壞人", "莫德雷德", "奧伯倫"]]

    if my_role in ["莫甘娜", "刺客", "壞人", "莫德雷德"]:
        visible = []
        for enemy_name in evil_team_names:
            enemy_obj = next(p for p in all_players if p['name'] == enemy_name)
            if enemy_obj['role'] != "奧伯倫" and enemy_obj['name'] != p_obj['name']:
                visible.append(enemy_name)
        info['teammates'] = visible
    
    elif my_role == "梅林":
        visible = []
        for enemy_name in evil_team_names:
            enemy_obj = next(p for p in all_players if p['name'] == enemy_name)
            if enemy_obj['role'] != "莫德雷德":
                visible.append(enemy_name)
        info['teammates'] = visible

    elif my_role == "派西維爾":
        targets = [p['name'] for p in all_players if p['role'] in ["梅林", "莫甘娜"]]
        random.shuffle(targets)
        info['teammates'] = targets

    await sio.emit('role_info', info, to=sid)

# ... (select_team, vote_team, vote_mission, assassinate, request_reset, reset_game, disconnect 邏輯保持 v6.0 不變)
# 為節省篇幅，請保留這些與 v6.0 相同的邏輯

@sio.event
async def select_team(sid, data):
    room_id = data['room_id']; team_tokens = data['team']; room = rooms[room_id]
    names = [room['players'][t]['name'] for t in team_tokens]
    await add_log(room_id, f"👑 提議: {', '.join(names)}", "#4fc3f7")
    room['current_team'] = team_tokens; room['state'] = GameState.TEAM_VOTING; room['votes'] = {}
    await play_sound(room_id, 'vote')
    await broadcast_state(room_id)

@sio.event
async def vote_team(sid, data):
    room_id = data['room_id']; vote = data['vote']; room = rooms[room_id]
    token = room['sid_map'].get(sid)
    if not token: return
    room['votes'][token] = vote
    if len(room['votes']) == len(room['players']):
        approves = list(room['votes'].values()).count(True); rejects = list(room['votes'].values()).count(False); passed = approves > rejects
        detail_str = " ".join([f"{room['players'][t]['name']}{'⭕' if v else '❌'}" for t, v in room['votes'].items()])
        await sio.emit('vote_finished', {'details': detail_str, 'pass': passed}, room=room_id)
        if passed:
            room['vote_track'] = 0; room['state'] = GameState.MISSION; room['mission_votes'] = []; room['mission_votes_who'] = []
            await add_log(room_id, f"✅ 通過 ({approves} vs {rejects})", "#66ff66")
        else:
            room['vote_track'] += 1; room['leader_index'] = (room['leader_index'] + 1) % len(room['players']); room['state'] = GameState.TEAM_SELECTION
            await play_sound(room_id, 'fail'); await add_log(room_id, f"⚠️ 否決 ({approves} vs {rejects}) - 失敗: {room['vote_track']}", "#ff6666")
            if room['vote_track'] >= 5: await add_log(room_id, "💀 5次流局，壞人勝！", "red"); room['state'] = GameState.GAME_OVER; await sio.emit('game_over', {'winner': 'RED (流局)'}, room=room_id)
        await broadcast_state(room_id)

@sio.event
async def vote_mission(sid, data):
    room_id = data['room_id']; result = data['result']; room = rooms[room_id]; token = room['sid_map'].get(sid)
    if token in room['current_team'] and token not in room['mission_votes_who']: room['mission_votes'].append(result); room['mission_votes_who'].append(token)
    if len(room['mission_votes']) == len(room['current_team']):
        fail_count = room['mission_votes'].count(False); is_fail = fail_count >= 1
        if len(room['players']) >= 7 and room['quest_index'] == 3: is_fail = fail_count >= 2
        is_success = not is_fail
        room['quest_results'][room['quest_index']] = is_success; result_text = "🛡️ 成功" if is_success else "🔥 失敗"
        await sio.emit('mission_effect', {'success': is_success}, room=room_id); await play_sound(room_id, 'success' if is_success else 'fail')
        await add_log(room_id, f"🏁 R{room['quest_index']+1}: {result_text} (黑卡: {fail_count})", "gold" if is_success else "red")
        room['quest_index'] += 1; room['leader_index'] = (room['leader_index'] + 1) % len(room['players']); room['state'] = GameState.TEAM_SELECTION
        wins = room['quest_results'].count(True); losses = room['quest_results'].count(False)
        if wins >= 3: room['state'] = GameState.ASSASSINATION; await add_log(room_id, "🗡️ 藍方3勝！刺客現身", "#ef5350")
        elif losses >= 3: room['state'] = GameState.GAME_OVER; await add_log(room_id, "💀 紅方3勝！壞人勝", "#ef5350"); await sio.emit('game_over', {'winner': 'RED (任務失敗)'}, room=room_id)
        await broadcast_state(room_id)

@sio.event
async def assassinate(sid, data):
    room_id = data['room_id']; target_token = data['target_token']; room = rooms[room_id]
    room['state'] = GameState.GAME_OVER; target_role = room['players'][target_token]['role']
    if target_role == "梅林": await add_log(room_id, "💀 梅林被殺！壞人勝！", "red"); await sio.emit('game_over', {'winner': 'RED (刺殺成功)'}, room=room_id)
    else: await add_log(room_id, f"🛡️ 刺殺失敗！好人勝！", "gold"); await sio.emit('game_over', {'winner': 'BLUE (刺殺失敗)'}, room=room_id)
    await broadcast_state(room_id)

@sio.event
async def request_reset(sid, room_id):
    if room_id not in rooms: return
    room = rooms[room_id]; token = room['sid_map'].get(sid)
    if token not in room['reset_votes']:
        room['reset_votes'].add(token); await add_log(room_id, f"⚠️ 請求重置 ({len(room['reset_votes'])}/{len(room['players'])})", "orange")
        if len(room['reset_votes']) > len(room['players']) / 2:
            room['state'] = GameState.LOBBY; room['quest_results'] = [None]*5; room['quest_index'] = 0; room['leader_index'] = 0
            room['current_team'] = []; room['votes'] = {}; room['mission_votes'] = []; room['mission_votes_who'] = []
            room['vote_track'] = 0; room['logs'] = []; room['reset_votes'] = set()
            for t in room['players']: room['players'][t]['role'] = None; room['players'][t]['is_ready'] = False
            await add_log(room_id, "🔄 遊戲已重置", "cyan")
        await broadcast_state(room_id)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app_asgi, host="0.0.0.0", port=port)
