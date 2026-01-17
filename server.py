import socketio
import random
import uvicorn
import uuid
import os
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])
app_asgi = socketio.ASGIApp(sio, app)
app.mount("/", StaticFiles(directory=".", html=True), name="static")

ROLES_CONFIG = {
    5: ["梅林", "派西維爾", "忠臣", "莫甘娜", "刺客"],
    6: ["梅林", "派西維爾", "忠臣", "忠臣", "莫甘娜", "刺客"],
    7: ["梅林", "派西維爾", "忠臣", "忠臣", "莫甘娜", "刺客", "奧伯倫"],
    8: ["梅林", "派西維爾", "忠臣", "忠臣", "忠臣", "莫甘娜", "刺客", "壞人"],
    9: ["梅林", "派西維爾", "忠臣", "忠臣", "忠臣", "忠臣", "莫甘娜", "刺客", "莫德雷德"],
    10: ["梅林", "派西維爾", "忠臣", "忠臣", "忠臣", "忠臣", "莫甘娜", "刺客", "莫德雷德", "奧伯倫"],
}

QUEST_CONFIG = {
    5: [2, 3, 2, 3, 3],
    6: [2, 3, 4, 3, 4],
    7: [2, 3, 3, 4, 4],
    8: [3, 4, 4, 5, 5],
    9: [3, 4, 4, 5, 5],
    10: [3, 4, 4, 5, 5],
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
    log_entry = {'time': timestamp, 'msg': message, 'color': color}
    rooms[room_id]['logs'].append(log_entry)
    await sio.emit('new_log', log_entry, room=room_id)


async def broadcast_state(room_id):
    room = rooms[room_id]
    players_list = []
    sorted_tokens = sorted(room['players'].keys(), key=lambda t: room['players'][t]['join_time'])

    for idx, token in enumerate(sorted_tokens):
        p = room['players'][token]
        has_voted = False
        if room['state'] == GameState.TEAM_VOTING:
            has_voted = token in room['votes']
        elif room['state'] == GameState.MISSION:
            has_voted = token in room['mission_votes_who']

        players_list.append({
            'token': token,
            'name': p['name'],
            'avatar': p['avatar'],
            'is_leader': idx == room['leader_index'],
            'in_team': token in room['current_team'],
            'has_voted': has_voted,
            'is_connected': p['connected'],
            'has_reset_voted': token in room['reset_votes']  # 顯示誰投了重置
        })

    required = 0
    try:
        required = QUEST_CONFIG[len(players_list)][room['quest_index']]
    except:
        pass

    data = {
        'state': room['state'],
        'players': players_list,
        'quest_results': room['quest_results'],
        'quest_idx': room['quest_index'],
        'team_size_needed': required,
        'vote_track': room['vote_track'],
        'logs': room['logs']
    }
    await sio.emit('update_state', data, room=room_id)


@sio.event
async def join_room(sid, data):
    name = data['name']
    room_id = data['room_id']
    avatar = data['avatar']
    token = data.get('token')

    if room_id not in rooms:
        rooms[room_id] = {
            'players': {}, 'sid_map': {},
            'state': GameState.LOBBY,
            'quest_results': [None] * 5,
            'quest_index': 0, 'leader_index': 0,
            'current_team': [], 'votes': {},
            'mission_votes': [], 'mission_votes_who': [],
            'vote_track': 0, 'logs': [],
            'reset_votes': set()  # 新增：重置投票箱
        }

    room = rooms[room_id]

    is_reconnect = False
    if token and token in room['players']:
        is_reconnect = True

    if is_reconnect:
        p = room['players'][token]
        p['sid'] = sid
        p['connected'] = True
        room['sid_map'][sid] = token
        p['name'] = name
        p['avatar'] = avatar
        await sio.enter_room(sid, room_id)
        await sio.emit('join_success', {'token': token}, to=sid)
        await add_log(room_id, f"⚡ {name} 重連", "#888")
        if p['role']: await send_role_info(sid, p, list(room['players'].values()))

    else:
        new_token = str(uuid.uuid4())
        room['players'][new_token] = {
            'token': new_token, 'name': name, 'avatar': avatar, 'sid': sid,
            'role': None, 'join_time': datetime.now().timestamp(), 'connected': True
        }
        room['sid_map'][sid] = new_token
        await sio.enter_room(sid, room_id)
        await sio.emit('join_success', {'token': new_token}, to=sid)
        await add_log(room_id, f"👋 {name} 加入", "#aaa")

    await broadcast_state(room_id)


@sio.event
async def disconnect(sid):
    for room_id, room in rooms.items():
        if sid in room['sid_map']:
            token = room['sid_map'][sid]
            if token in room['players']:
                room['players'][token]['connected'] = False
                await broadcast_state(room_id)
            break


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


@sio.event
async def start_game(sid, room_id):
    room = rooms.get(room_id)
    if not room: return

    sorted_tokens = sorted(room['players'].keys(), key=lambda t: room['players'][t]['join_time'])
    players_objs = [room['players'][t] for t in sorted_tokens]
    cnt = len(players_objs)
    roles = ROLES_CONFIG.get(cnt, ["好人"] * cnt)
    random.shuffle(roles)

    # 清空重置投票
    room['reset_votes'] = set()
    room['state'] = GameState.TEAM_SELECTION
    room['quest_index'] = 0
    room['leader_index'] = 0
    room['quest_results'] = [None] * 5
    room['vote_track'] = 0
    room['logs'] = []

    for i, p_obj in enumerate(players_objs):
        p_obj['role'] = roles[i]

    for p_obj in players_objs:
        await send_role_info(p_obj['sid'], p_obj, players_objs)

    await add_log(room_id, "🎮 遊戲開始！", "gold")
    await broadcast_state(room_id)


@sio.event
async def select_team(sid, data):
    room_id = data['room_id']
    team_tokens = data['team']
    room = rooms[room_id]
    names = [room['players'][t]['name'] for t in team_tokens]
    await add_log(room_id, f"👑 提議: {', '.join(names)}", "#4fc3f7")
    room['current_team'] = team_tokens
    room['state'] = GameState.TEAM_VOTING
    room['votes'] = {}
    await broadcast_state(room_id)


@sio.event
async def vote_team(sid, data):
    room_id = data['room_id']
    vote = data['vote']
    room = rooms[room_id]
    token = room['sid_map'].get(sid)
    if not token: return

    room['votes'][token] = vote

    if len(room['votes']) == len(room['players']):
        approves = list(room['votes'].values()).count(True)
        rejects = list(room['votes'].values()).count(False)
        passed = approves > rejects

        detail_str = " ".join([f"{room['players'][t]['name']}{'⭕' if v else '❌'}" for t, v in room['votes'].items()])
        await sio.emit('vote_finished', {'details': detail_str, 'pass': passed}, room=room_id)

        if passed:
            room['vote_track'] = 0
            room['state'] = GameState.MISSION
            room['mission_votes'] = []
            room['mission_votes_who'] = []
            await add_log(room_id, f"✅ 通過 ({approves} vs {rejects})", "#66ff66")
        else:
            room['vote_track'] += 1
            room['leader_index'] = (room['leader_index'] + 1) % len(room['players'])
            room['state'] = GameState.TEAM_SELECTION
            await add_log(room_id, f"⚠️ 否決 ({approves} vs {rejects}) - 失敗: {room['vote_track']}", "#ff6666")
            if room['vote_track'] >= 5:
                await add_log(room_id, "💀 5次流局，壞人勝！", "red")
                room['state'] = GameState.GAME_OVER
                await sio.emit('game_over', {'winner': 'RED (流局)'}, room=room_id)

        await broadcast_state(room_id)


@sio.event
async def vote_mission(sid, data):
    room_id = data['room_id']
    result = data['result']
    room = rooms[room_id]
    token = room['sid_map'].get(sid)

    if token in room['current_team'] and token not in room['mission_votes_who']:
        room['mission_votes'].append(result)
        room['mission_votes_who'].append(token)

    if len(room['mission_votes']) == len(room['current_team']):
        fail_count = room['mission_votes'].count(False)
        is_fail = fail_count >= 1
        if len(room['players']) >= 7 and room['quest_index'] == 3: is_fail = fail_count >= 2
        is_success = not is_fail

        room['quest_results'][room['quest_index']] = is_success
        result_text = "🛡️ 成功" if is_success else "🔥 失敗"

        await sio.emit('mission_effect', {'success': is_success}, room=room_id)
        await add_log(room_id, f"🏁 R{room['quest_index'] + 1}: {result_text} (黑卡: {fail_count})",
                      "gold" if is_success else "red")

        room['quest_index'] += 1
        room['leader_index'] = (room['leader_index'] + 1) % len(room['players'])
        room['state'] = GameState.TEAM_SELECTION

        wins = room['quest_results'].count(True)
        losses = room['quest_results'].count(False)

        if wins >= 3:
            room['state'] = GameState.ASSASSINATION
            await add_log(room_id, "🗡️ 藍方3勝！刺客現身", "#ef5350")
        elif losses >= 3:
            room['state'] = GameState.GAME_OVER
            await add_log(room_id, "💀 紅方3勝！壞人勝", "#ef5350")
            await sio.emit('game_over', {'winner': 'RED (任務失敗)'}, room=room_id)

        await broadcast_state(room_id)


@sio.event
async def assassinate(sid, data):
    room_id = data['room_id']
    target_token = data['target_token']
    room = rooms[room_id]

    room['state'] = GameState.GAME_OVER
    target_role = room['players'][target_token]['role']

    if target_role == "梅林":
        await add_log(room_id, "💀 梅林被殺！壞人勝！", "red")
        await sio.emit('game_over', {'winner': 'RED (刺殺成功)'}, room=room_id)
    else:
        await add_log(room_id, f"🛡️ 刺殺失敗！好人勝！", "gold")
        await sio.emit('game_over', {'winner': 'BLUE (刺殺失敗)'}, room=room_id)

    await broadcast_state(room_id)


# === 修改：重置投票邏輯 ===
@sio.event
async def request_reset(sid, room_id):
    if room_id not in rooms: return
    room = rooms[room_id]
    token = room['sid_map'].get(sid)
    if not token: return

    # 紀錄該玩家投票
    if token not in room['reset_votes']:
        room['reset_votes'].add(token)
        player_name = room['players'][token]['name']
        vote_count = len(room['reset_votes'])
        total_players = len(room['players'])

        await add_log(room_id, f"⚠️ {player_name} 請求重置 ({vote_count}/{total_players})", "orange")

        # 檢查是否過半
        if vote_count > total_players / 2:
            # 執行重置
            room['state'] = GameState.LOBBY
            room['quest_results'] = [None] * 5
            room['quest_index'] = 0
            room['leader_index'] = 0
            room['current_team'] = []
            room['votes'] = {}
            room['mission_votes'] = []
            room['mission_votes_who'] = []
            room['vote_track'] = 0
            room['logs'] = []
            room['reset_votes'] = set()  # 清空投票

            # 清除角色
            for t in room['players']:
                room['players'][t]['role'] = None

            await add_log(room_id, "🔄 玩家過半同意，遊戲已重置", "cyan")

        await broadcast_state(room_id)


if __name__ == '__main__':
    # 2. 修改這裡：讀取環境變數 PORT，如果沒有（例如在自己電腦）才用 8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app_asgi, host="0.0.0.0", port=port)
