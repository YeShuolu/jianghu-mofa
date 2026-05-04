"""
江湖魔法师 · FastAPI 服务器 v2
新增：跨局玩家保留 + 多局赛制 + 超时定时器 + 改名 + 投票返回大厅
"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.game import Game, Player, FIRST_DECISION_SECONDS, SUBSEQUENT_DECISION_SECONDS

EXIT_VOTE_TIMEOUT = 30  # 投票退出的超时秒数


class Room:
    def __init__(self):
        self.game: Optional[Game] = None
        self.sockets: dict[int, WebSocket] = {}
        # 跨局保留的玩家：seat -> Player（含分数、姓名）
        self.players: dict[int, Player] = {}
        self.lock = asyncio.Lock()
        # 赛制
        self.total_rounds: int = 3
        self.current_round: int = 0  # 已完成的局数
        # 投票
        self.exit_votes: set[int] = set()
        self.exit_vote_active: bool = False
        self.exit_vote_deadline: Optional[float] = None  # 投票截止时间戳
        self.exit_vote_timer: Optional[asyncio.Task] = None
        # 超时定时任务（喊牌）
        self.timer_task: Optional[asyncio.Task] = None

    def free_seats(self) -> list[int]:
        return [i for i in range(6) if i not in self.players]

    def occupied(self) -> int:
        return len(self.players)

    def in_game(self) -> bool:
        return self.game is not None and self.game.started and not self.game.game_over

    def reset_all(self):
        self.game = None
        self.players.clear()
        self.exit_votes.clear()
        self.exit_vote_active = False
        self.current_round = 0


room = Room()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🎮 江湖魔法师服务器启动")
    yield
    print("服务器关闭")
    if room.timer_task: room.timer_task.cancel()


app = FastAPI(lifespan=lifespan)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/room")
async def room_status():
    return {"occupied": room.occupied(), "in_game": room.in_game()}


@app.post("/api/reset")
async def reset_room():
    """彻底重置（管理员）"""
    async with room.lock:
        if room.timer_task:
            room.timer_task.cancel()
            room.timer_task = None
        for ws in list(room.sockets.values()):
            try: await ws.close()
            except: pass
        room.sockets.clear()
        room.reset_all()
    return {"ok": True}


# ============= WebSocket =============
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, seat: Optional[int] = Query(None)):
    await ws.accept()
    pid: Optional[int] = None
    try:
        async with room.lock:
            if seat is not None:
                if not (0 <= seat < 6):
                    await ws.send_json({"type": "error", "msg": "座位号无效"})
                    await ws.close(); return
                if seat in room.players:
                    # 已有人占该座 → 视为重连（关闭旧 ws 并接管）
                    if seat in room.sockets:
                        old = room.sockets[seat]
                        try: await old.close()
                        except: pass
                    pid = seat
                else:
                    if room.in_game():
                        await ws.send_json({"type": "error", "msg": "游戏中无法加入新座位"})
                        await ws.close(); return
                    # 占新座
                    pid = seat
                    name = f"玩家{chr(ord('A') + pid)}"
                    room.players[pid] = Player(pid, name)
            else:
                if room.in_game():
                    await ws.send_json({"type": "error", "msg": "游戏中无法加入"})
                    await ws.close(); return
                free = room.free_seats()
                if not free:
                    await ws.send_json({"type": "error", "msg": "房间已满"})
                    await ws.close(); return
                pid = free[0]
                room.players[pid] = Player(pid, f"玩家{chr(ord('A') + pid)}")

            room.sockets[pid] = ws
            room.players[pid].connected = True
            # 同步到 game 对象
            if room.game and pid < len(room.game.players):
                room.game.players[pid].connected = True

        await ws.send_json({"type": "joined", "pid": pid})
        await broadcast_state_or_lobby()

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_message(pid, msg, ws)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WS error pid={pid}: {e}")
    finally:
        async with room.lock:
            if pid is not None and room.sockets.get(pid) is ws:
                del room.sockets[pid]
                # 不删除 room.players[pid]：跨局保留分数与名字，仅断开 socket
                if pid in room.players:
                    room.players[pid].connected = False
                if room.game and pid < len(room.game.players):
                    room.game.players[pid].connected = False
                # 如果还没开始游戏，且玩家走了，从座位移除（防止座位被占住没人）
                if not room.in_game() and not room.game:
                    if pid in room.players:
                        del room.players[pid]
                # 投票期间断线视为放弃（不计票）
                room.exit_votes.discard(pid)
                if room.exit_vote_active and not room.exit_votes:
                    _stop_exit_vote_timer()
                    room.exit_vote_active = False
                    room.exit_vote_deadline = None
        await broadcast_state_or_lobby()


async def handle_message(pid: int, msg: dict, ws: WebSocket):
    t = msg.get("type")

    if t == "set_name":
        name = (msg.get("name") or "").strip()[:12]
        if not name: return
        async with room.lock:
            if pid in room.players:
                room.players[pid].name = name
            if room.game and pid < len(room.game.players):
                room.game.players[pid].name = name
        await broadcast_state_or_lobby()
        return

    if t == "set_rounds":
        n = msg.get("n")
        if isinstance(n, int) and 1 <= n <= 9:
            async with room.lock:
                if not room.in_game():
                    room.total_rounds = n
            await broadcast_state_or_lobby()
        return

    if t == "start_game":
        async with room.lock:
            if room.in_game():
                await ws.send_json({"type": "error", "msg": "本局尚未结束"})
                return
            occ = room.occupied()
            if occ < 3:
                await ws.send_json({"type": "error", "msg": f"至少需要 3 人才能开始（当前 {occ} 人）"})
                return
            taken_idx = sorted(room.players.keys())
            if taken_idx != list(range(len(taken_idx))):
                await ws.send_json({"type": "error", "msg": f"座位不连续，请确保从 1 号位起依次落座"})
                return
            # 第一局开始：清零分数和已完成局数（如果之前是新房间）
            if room.current_round == 0:
                for p in room.players.values():
                    p.score = 0
            # 开局
            game_players = [room.players[i] for i in taken_idx]
            room.game = Game(game_players)
            # 起始玩家：每局轮换（按已完成局数）
            first = room.current_round % len(game_players)
            room.game.start(first_player=first)
            _start_turn_timer()
        await broadcast_state()
        return

    if t == "next_round":
        # 上一局结束后，准备下一局
        async with room.lock:
            if not room.game or not room.game.game_over:
                await ws.send_json({"type": "error", "msg": "上一局尚未结束"})
                return
            # 如果还没到总局数，开下一局
            if room.current_round >= room.total_rounds:
                await ws.send_json({"type": "error", "msg": "整轮已结束"})
                return
            # 重新使用当前玩家列表
            taken_idx = sorted(room.players.keys())
            if len(taken_idx) < 3:
                await ws.send_json({"type": "error", "msg": "玩家不足 3 人，无法开始下一局"})
                return
            game_players = [room.players[i] for i in taken_idx]
            room.game = Game(game_players)
            first = room.current_round % len(game_players)
            room.game.start(first_player=first)
            _start_turn_timer()
        await broadcast_state()
        return

    if t == "reset_match":
        # 重置整个赛制（清零分数、回到大厅状态）
        async with room.lock:
            for p in room.players.values():
                p.score = 0
            room.current_round = 0
            room.game = None
            room.exit_votes.clear()
            room.exit_vote_active = False
            _stop_turn_timer()
        await broadcast_state_or_lobby()
        return

    if t == "return_to_lobby":
        # 整轮打完后，任何人可点 → 全员回大厅 + 分数清零
        async with room.lock:
            match_done = (room.current_round >= room.total_rounds and room.current_round > 0)
            if not match_done:
                await ws.send_json({"type": "error", "msg": "整轮尚未打完，无法返回大厅"})
                return
            for p in room.players.values():
                p.score = 0
            room.current_round = 0
            room.game = None
            room.exit_votes.clear()
            room.exit_vote_active = False
            _stop_turn_timer()
        await broadcast_state_or_lobby()
        return

    if t == "vote_exit":
        async with room.lock:
            if not room.in_game():
                await ws.send_json({"type": "error", "msg": "无需投票（不在游戏中）"})
                return
            yes = bool(msg.get("yes", True))
            if yes:
                # 第一票：启动倒计时
                if not room.exit_vote_active:
                    room.exit_vote_active = True
                    room.exit_vote_deadline = time.time() + EXIT_VOTE_TIMEOUT
                    _start_exit_vote_timer()
                room.exit_votes.add(pid)
            else:
                room.exit_votes.discard(pid)
                # 没人投了，结束投票
                if not room.exit_votes:
                    _stop_exit_vote_timer()
                    room.exit_vote_active = False
                    room.exit_vote_deadline = None
            # 达到 2 人 → 通过
            if len(room.exit_votes) >= 2:
                _stop_exit_vote_timer()
                room.game._log("🚪 投票通过：本局中止，赛制重置，所有分数清零", "event")
                room.game.game_over = True
                room.game.winner_id = None
                room.exit_votes.clear()
                room.exit_vote_active = False
                room.exit_vote_deadline = None
                _stop_turn_timer()
                # 清零所有玩家的分数
                for p in room.players.values():
                    p.score = 0
                # 已完成局数也清零（赛制完全重置）
                room.current_round = 0
        await broadcast_state_or_lobby()
        return

    # 以下需要游戏中
    if not room.in_game():
        await ws.send_json({"type": "error", "msg": "游戏未开始或已结束"})
        return

    if t == "call":
        n = msg.get("n")
        if not isinstance(n, int): return
        async with room.lock:
            res = room.game.call_number(pid, n)
        if not res.get("ok"):
            await ws.send_json({"type": "error", "msg": _reason_to_text(res.get("reason"))})
            return
        await _on_action_done()
        return

    if t == "pass":
        async with room.lock:
            res = room.game.pass_turn(pid)
        if not res.get("ok"):
            await ws.send_json({"type": "error", "msg": _reason_to_text(res.get("reason"))})
            return
        await _on_action_done()
        return

    if t == "confirm_dice":
        async with room.lock:
            room.game.confirm_dice(pid)
        await _on_action_done()
        return

    if t == "choose_mystery_peek":
        idx = msg.get("idx")
        if not isinstance(idx, int): return
        async with room.lock:
            room.game.choose_mystery_peek(pid, idx)
        await _on_action_done()
        return

    if t == "confirm_peek_mystery":
        async with room.lock:
            room.game.confirm_peek_mystery(pid)
        await _on_action_done()
        return


def _reason_to_text(reason):
    return {
        "game_over": "游戏已结束",
        "pending_action": "请先完成当前动作",
        "not_your_turn": "还没轮到你",
        "invalid_number": "无效号码",
        "below_min_callable": "号码必须 ≥ 当前最小可喊号",
        "must_call_at_least_once": "本回合必须先成功喊出至少一张牌才能过牌",
    }.get(reason or "", reason or "未知错误")


# ============= 超时定时器 =============
def _stop_exit_vote_timer():
    if room.exit_vote_timer:
        room.exit_vote_timer.cancel()
        room.exit_vote_timer = None


def _start_exit_vote_timer():
    """投票发起后启动 30 秒倒计时；过期自动作废所有票"""
    _stop_exit_vote_timer()
    deadline = room.exit_vote_deadline

    async def _wait():
        try:
            await asyncio.sleep(EXIT_VOTE_TIMEOUT)
            async with room.lock:
                # 二次校验：仍然是这次投票（没有被通过 / 取消）
                if not room.exit_vote_active: return
                if room.exit_vote_deadline != deadline: return
                # 投票超时作废
                if room.game:
                    room.game._log("⌛ 投票超时，所有票作废", "event")
                room.exit_votes.clear()
                room.exit_vote_active = False
                room.exit_vote_deadline = None
            await broadcast_state_or_lobby()
        except asyncio.CancelledError:
            pass

    room.exit_vote_timer = asyncio.create_task(_wait())


def _stop_turn_timer():
    if room.timer_task:
        room.timer_task.cancel()
        room.timer_task = None
    if room.game:
        room.game.turn_deadline = None


def _start_turn_timer():
    """启动当前回合（或当前 pending）的超时
    - 一回合的第一次决策：45 秒
    - 同回合后续决策（喊对后继续、处理弹窗）：30 秒
    """
    _stop_turn_timer()
    if not room.game or room.game.game_over:
        return
    # 判断这是不是当前回合的第一次决策
    # 标志：has_called_this_turn=False 且没有 pending → 一定是回合开头
    is_first = (not room.game.has_called_this_turn) and (room.game.pending is None)
    duration = FIRST_DECISION_SECONDS if is_first else SUBSEQUENT_DECISION_SECONDS
    deadline = time.time() + duration
    room.game.turn_deadline = deadline

    async def _wait():
        try:
            await asyncio.sleep(duration)
            async with room.lock:
                if not room.game or room.game.game_over: return
                if room.game.turn_deadline != deadline: return
                if room.game.pending is not None:
                    p = room.game.players[room.game.pending.get("player_id", 0)]
                    room.game._log(f"⏰ {p.name} 决策超时，扣 1 血", "damage")
                    room.game._damage(p, 1)
                    room.game.pending = None
                    room.game._end_turn()
                else:
                    room.game.timeout_current()
                room.game.turn_deadline = None
            await _on_action_done()
        except asyncio.CancelledError:
            pass

    room.timer_task = asyncio.create_task(_wait())


async def _on_action_done():
    """每次有效操作后调用：广播状态 + 重启计时器"""
    if room.game:
        if room.game.game_over:
            _stop_turn_timer()
            # 如果不是投票退出 → 算一局完成 + 加分
            if room.game.winner_id is not None:
                room.current_round += 1
                winner = room.game.players[room.game.winner_id]
                winner.score += 1
            await broadcast_state()
        else:
            _start_turn_timer()
            await broadcast_state()
    else:
        await broadcast_lobby()


# ============= 广播 =============
async def broadcast_state():
    if not room.game: return
    extra = _match_meta()
    for pid, ws in list(room.sockets.items()):
        try:
            data = room.game.state_for(pid)
            data.update(extra)
            await ws.send_json({"type": "state", "data": data})
        except: pass


async def broadcast_lobby():
    seats = []
    for i in range(6):
        if i in room.players:
            p = room.players[i]
            seats.append({
                "seat": i, "taken": True, "online": (i in room.sockets),
                "name": p.name, "score": p.score
            })
        else:
            seats.append({"seat": i, "taken": False, "online": False, "name": None, "score": 0})
    base = {
        "type": "lobby",
        "data": {
            "occupied": room.occupied(),
            "seats": seats,
            "in_game": room.in_game(),
            "total_rounds": room.total_rounds,
            "current_round": room.current_round,
            "match_done": (room.current_round >= room.total_rounds and room.current_round > 0),
        }
    }
    for pid, ws in list(room.sockets.items()):
        try:
            payload = {"type": "lobby", "data": dict(base["data"])}
            payload["data"]["you"] = pid
            await ws.send_json(payload)
        except: pass


async def broadcast_state_or_lobby():
    if room.in_game():
        await broadcast_state()
    else:
        await broadcast_lobby()


def _match_meta():
    return {
        "total_rounds": room.total_rounds,
        "current_round": room.current_round,
        "exit_votes": list(room.exit_votes),
        "exit_vote_active": room.exit_vote_active,
        "exit_vote_deadline": room.exit_vote_deadline,
        "match_done": (room.current_round >= room.total_rounds and room.current_round > 0),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
