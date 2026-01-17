import socketio
import random
import uvicorn
from datetime import datetime
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# === 基礎設定 ===
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = FastAPI()

# 允許跨域 (開發方便)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])
app_asgi = socketio.ASGIApp(sio, app)

# === 讓伺服器可以直接讀取當前目錄下的 index.html ===
# 注意：index.html 必須和 server.py 在同一個資料夾
app.mount("/", StaticFiles(directory=".", html=True), name="static")

# === 遊戲規則配置 ===
ROLES_CONFIG = {
    5: ["梅林", "派西維爾", "忠臣", "莫甘娜", "刺客"],
    6: ["梅林", "派西維爾", "忠臣", "忠臣", "莫甘娜", "刺客"],
    7: ["梅林", "派西維爾", "忠臣", "忠臣", "莫甘娜", "刺客", "奧伯倫"],
    8: ["梅林", "派西維爾", "忠臣", "忠臣", "忠臣", "莫甘娜", "刺客", "壞人"],
    9: ["梅林", "派西維爾", "忠臣", "忠臣", "忠臣", "忠臣", "莫甘娜", "刺客", "莫德雷德"],
    10: ["梅林", "派西維爾", "忠臣", "忠臣", "忠臣", "忠臣", "莫甘娜", "刺客", "莫德雷德", "奧伯倫"],
}

# 任務人數配置 [任務1, 任務2, 任務3, 任務4, 任務5]
QUEST_CONFIG = {
    5: [2, 3, 2, 3, 3],
    6: [2, 3, 4, 3, 4],
    7: [2, 3, 3, 4, 4],  # 第4局需2張失敗 (程式碼邏輯有處理)
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
    GAME_OVER = 'GAME_OVER'


# === 輔助功能 ===

async def add_log(room_id, message, color='white'):
    """新增遊戲紀錄並廣播"""
    if room_id not in rooms: return
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = {'time': timestamp, 'msg': message, 'color': color}
    rooms[room_id]['logs'].append(log_entry)
    await sio.emit('new_log', log_entry, room=room_id)


async def broadcast_state(room_id):
    """同步房間狀態"""
    room = rooms[room_id]
    players_list = []

    # 依照加入時間排序，保證座位順序一致
    p_ids = sorted(room['players'].keys(), key=lambda x: room['players'][x]['join_time'])

    for idx, pid in enumerate(p_ids):
        p = room['players'][pid]
        players_list.append({
            'sid': pid,
            'name': p['name'],
            'avatar': p['avatar'],
            'is_leader': idx == room['leader_index'],
            'in_team': pid in room['current_team'],
            # 狀態隱私保護：投票階段只顯示"已投"，不顯示"投什麼"
            'has_voted': (pid in room['votes']) if room['state'] == GameState.TEAM_VOTING else (
                        pid in room['mission_votes_who'])
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
        'logs': room['logs']  # 斷線重連補償
    }
    await sio.emit('update_state', data, room=room_id)


# === Socket 事件處理 ===

@sio.event
async def join_room(sid, data):
    name = data['name']
    room_id = data['room_id']
    avatar = data['avatar']

    if room_id not in rooms:
        rooms[room_id] = {
            'players': {},
            'state': GameState.LOBBY,
            'quest_results': [None] * 5,
            'quest_index': 0,
            'leader_index': 0,
            'current_team': [],
            'votes': {},
            'mission_votes': [],
            'mission_votes_who': [],
            'vote_track': 0,  # 連續失敗次數
            'logs': []
        }

    room = rooms[room_id]
    room['players'][sid] = {
        'name': name,
        'avatar': avatar,
        'sid': sid,
        'role': None,
        'join_time': datetime.now().timestamp()
    }

    sio.enter_room(sid, room_id)
    await add_log(room_id, f"👋 {name} 加入了房間", "#aaa")
    await broadcast_state(room_id)


@sio.event
async def start_game(sid, room_id):
    room = rooms.get(room_id)
    if not room: return

    # 將玩家轉為列表並排序
    players_sids = sorted(room['players'].keys(), key=lambda x: room['players'][x]['join_time'])
    players_objs = [room['players'][sid] for sid in players_sids]
    cnt = len(players_objs)

    # 簡單防呆，正式玩建議取消註解
    # if cnt < 5: return

    roles = ROLES_CONFIG.get(cnt, ["好人"] * cnt)
    random.shuffle(roles)

    # 重置數據
    room['state'] = GameState.TEAM_SELECTION
    room['quest_index'] = 0
    room['leader_index'] = 0
    room['quest_results'] = [None] * 5
    room['vote_track'] = 0
    room['logs'] = []

    # 先分配身分
    evil_team_names = []
    for i, p_obj in enumerate(players_objs):
        role = roles[i]
        p_obj['role'] = role
        if role in ["莫甘娜", "刺客", "壞人", "莫德雷德", "奧伯倫"]:
            evil_team_names.append(p_obj['name'])

    # 個別發送身分資訊 (視野邏輯)
    for p_obj in players_objs:
        my_role = p_obj['role']
        info = {'role': my_role, 'teammates': []}

        # 1. 壞人視野 (奧伯倫除外)
        if my_role in ["莫甘娜", "刺客", "壞人", "莫德雷德"]:
            # 看到除了奧伯倫以外的所有壞人
            visible = []
            for enemy_name in evil_team_names:
                enemy_obj = next(p for p in players_objs if p['name'] == enemy_name)
                if enemy_obj['role'] != "奧伯倫" and enemy_obj['name'] != p_obj['name']:
                    visible.append(enemy_name)
            info['teammates'] = visible

        # 2. 梅林視野
        elif my_role == "梅林":
            # 看到除了莫德雷德以外的所有壞人
            visible = []
            for enemy_name in evil_team_names:
                enemy_obj = next(p for p in players_objs if p['name'] == enemy_name)
                if enemy_obj['role'] != "莫德雷德":
                    visible.append(enemy_name)
            info['teammates'] = visible

        # 3. 派西維爾視野
        elif my_role == "派西維爾":
            # 看到梅林和莫甘娜 (不知誰是誰)
            targets = [p['name'] for p in players_objs if p['role'] in ["梅林", "莫甘娜"]]
            random.shuffle(targets)
            info['teammates'] = targets

        # 4. 奧伯倫視野 (空)

        await sio.emit('role_info', info, to=p_obj['sid'])

    await add_log(room_id, "🎮 遊戲開始！請查看右下角身分卡。", "gold")
    await broadcast_state(room_id)


@sio.event
async def select_team(sid, data):
    room_id = data['room_id']
    team_sids = data['team']
    room = rooms[room_id]

    names = [room['players'][s]['name'] for s in team_sids]
    await add_log(room_id, f"👑 隊長提議: {', '.join(names)}", "#4fc3f7")

    room['current_team'] = team_sids
    room['state'] = GameState.TEAM_VOTING
    room['votes'] = {}
    await broadcast_state(room_id)


@sio.event
async def vote_team(sid, data):
    room_id = data['room_id']
    vote = data['vote']
    room = rooms[room_id]

    room['votes'][sid] = vote

    # 結算投票
    if len(room['votes']) == len(room['players']):
        approves = list(room['votes'].values()).count(True)
        rejects = list(room['votes'].values()).count(False)
        passed = approves > rejects

        # 紀錄明細
        detail_str = " ".join([f"{room['players'][k]['name']}{'⭕' if v else '❌'}" for k, v in room['votes'].items()])
        await sio.emit('vote_finished',
                       {'details': {room['players'][k]['name']: v for k, v in room['votes'].items()}, 'pass': passed},
                       room=room_id)

        if passed:
            room['vote_track'] = 0
            room['state'] = GameState.MISSION
            room['mission_votes'] = []
            room['mission_votes_who'] = []
            await add_log(room_id, f"✅ 隊伍通過 ({approves} vs {rejects})", "#66ff66")
            await add_log(room_id, "🚀 任務執行中...", "#aaa")
        else:
            room['vote_track'] += 1
            room['leader_index'] = (room['leader_index'] + 1) % len(room['players'])
            room['state'] = GameState.TEAM_SELECTION
            await add_log(room_id, f"⚠️ 否決 ({approves} vs {rejects}) - 失敗次數: {room['vote_track']}", "#ff6666")

            if room['vote_track'] >= 5:
                await add_log(room_id, "💀 連續 5 次流局，壞人獲勝！", "red")
                room['state'] = GameState.GAME_OVER
                await sio.emit('game_over', {'winner': 'RED (連續流局)'}, room=room_id)

        await broadcast_state(room_id)


@sio.event
async def vote_mission(sid, data):
    room_id = data['room_id']
    result = data['result']  # True=成功, False=失敗
    room = rooms[room_id]

    if sid in room['current_team'] and sid not in room['mission_votes_who']:
        room['mission_votes'].append(result)
        room['mission_votes_who'].append(sid)

    if len(room['mission_votes']) == len(room['current_team']):
        fail_count = room['mission_votes'].count(False)

        # 規則檢查：7人以上第4局需2張失敗
        is_fail = fail_count >= 1
        if len(room['players']) >= 7 and room['quest_index'] == 3:
            is_fail = fail_count >= 2

        is_success = not is_fail

        room['quest_results'][room['quest_index']] = is_success
        result_text = "🛡️ 成功" if is_success else "🔥 失敗"
        await add_log(room_id, f"🏁 第 {room['quest_index'] + 1} 局: {result_text} (黑卡: {fail_count})",
                      "gold" if is_success else "red")

        room['quest_index'] += 1
        room['leader_index'] = (room['leader_index'] + 1) % len(room['players'])
        room['state'] = GameState.TEAM_SELECTION

        # 檢查勝負
        wins = room['quest_results'].count(True)
        losses = room['quest_results'].count(False)

        if wins >= 3:
            room['state'] = GameState.GAME_OVER
            await add_log(room_id, "🏆 藍方3勝！進入刺殺環節！", "#4fc3f7")
            await sio.emit('game_over', {'winner': 'BLUE (等待刺殺)'}, room=room_id)
        elif losses >= 3:
            room['state'] = GameState.GAME_OVER
            await add_log(room_id, "💀 紅方3勝！壞人獲勝！", "#ef5350")
            await sio.emit('game_over', {'winner': 'RED (壞人獲勝)'}, room=room_id)

        await broadcast_state(room_id)


if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=8000)