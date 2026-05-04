"""
江湖魔法师 · FastAPI 服务器 v3
v3 新增：
- 机器人玩家（add_bot / kick_bot，AI 自动决策有思考延迟）
- 踢出真人玩家（仅大厅，由其他玩家发起）
- 观战模式（满座或主动以观战加入）
"""
import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.game import Game, Player, FIRST_DECISION_SECONDS, SUBSEQUENT_DECISION_SECONDS

EXIT_VOTE_TIMEOUT = 30  # 投票退出的超时秒数
BOT_THINK_MIN = 0.8     # 机器人最少思考时间
BOT_THINK_MAX = 1.8     # 机器人最多思考时间


class Room:
    def __init__(self):
        self.game: Optional[Game] = None
        self.sockets: dict[int, WebSocket] = {}  # 真人玩家 socket（座位号 → ws）
        self.spectator_sockets: set[WebSocket] = set()  # 观战者 ws 集合
        # 跨局保留的玩家：seat -> Player（含分数、姓名、是否机器人）
        self.players: dict[int, Player] = {}
        self.lock = asyncio.Lock()
        # 赛制
        self.total_rounds: int = 3
        self.current_round: int = 0
        # 投票
        self.exit_votes: set[int] = set()
        self.exit_vote_active: bool = False
        self.exit_vote_deadline: Optional[float] = None
        self.exit_vote_timer: Optional[asyncio.Task] = None
        # 超时定时任务
        self.timer_task: Optional[asyncio.Task] = None
        # 机器人异步任务（防止重复触发）
        self.bot_task: Optional[asyncio.Task] = None

    def free_seats(self) -> list[int]:
        return [i for i in range(6) if i not in self.players]

    def occupied(self) -> int:
        return len(self.players)

    def human_count(self) -> int:
        return sum(1 for p in self.players.values() if not p.is_bot)

    def bot_seats(self) -> list[int]:
        return sorted([s for s, p in self.players.items() if p.is_bot])

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
    print("🎮 江湖魔法师服务器启动 (v3 · 含机器人/观战/踢人)")
    yield
    print("服务器关闭")
    if room.timer_task: room.timer_task.cancel()
    if room.bot_task: room.bot_task.cancel()


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
        if room.bot_task:
            room.bot_task.cancel()
            room.bot_task = None
        for ws in list(room.sockets.values()):
            try: await ws.close()
            except: pass
        for ws in list(room.spectator_sockets):
            try: await ws.close()
            except: pass
        room.sockets.clear()
        room.spectator_sockets.clear()
        room.reset_all()
    return {"ok": True}


# ============= WebSocket =============
@app.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    seat: Optional[int] = Query(None),
    spectate: Optional[int] = Query(None),
):
    await ws.accept()
    pid: Optional[int] = None
    is_spectator = False
    try:
        async with room.lock:
            # 主动观战
            if spectate:
                is_spectator = True
                room.spectator_sockets.add(ws)
            elif seat is not None:
                if not (0 <= seat < 6):
                    await ws.send_json({"type": "error", "msg": "座位号无效"})
                    await ws.close(); return
                if seat in room.players:
                    p = room.players[seat]
                    if p.is_bot:
                        # 试图占用机器人座位 → 拒绝（应先踢机器人再占）
                        await ws.send_json({"type": "error", "msg": "该座位被机器人占据，请先踢出机器人"})
                        await ws.close(); return
                    # 真人座位重连
                    if seat in room.sockets:
                        old = room.sockets[seat]
                        try: await old.close()
                        except: pass
                    pid = seat
                else:
                    if room.in_game():
                        # 游戏中无法新加入座位，自动转为观战
                        is_spectator = True
                        room.spectator_sockets.add(ws)
                    else:
                        pid = seat
                        name = f"玩家{chr(ord('A') + pid)}"
                        room.players[pid] = Player(pid, name, is_bot=False)
            else:
                # 没指定座位：找空座，找不到则观战
                if room.in_game():
                    is_spectator = True
                    room.spectator_sockets.add(ws)
                else:
                    free = room.free_seats()
                    if not free:
                        # 房间满 → 观战
                        is_spectator = True
                        room.spectator_sockets.add(ws)
                    else:
                        pid = free[0]
                        room.players[pid] = Player(pid, f"玩家{chr(ord('A') + pid)}", is_bot=False)

            if not is_spectator:
                room.sockets[pid] = ws
                room.players[pid].connected = True
                if room.game and pid < len(room.game.players):
                    room.game.players[pid].connected = True

        if is_spectator:
            await ws.send_json({"type": "joined_spectator"})
        else:
            await ws.send_json({"type": "joined", "pid": pid})
        await broadcast_state_or_lobby()

        # 加入后立即检查是否需要触发机器人
        if not is_spectator:
            await _maybe_trigger_bot()

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if is_spectator:
                # 观战者只能发"切换为玩家"等极少数消息（暂不支持，所以忽略）
                t = msg.get("type")
                if t == "leave_spectator":
                    break
                continue
            await handle_message(pid, msg, ws)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WS error pid={pid} spectator={is_spectator}: {e}")
    finally:
        async with room.lock:
            if is_spectator:
                room.spectator_sockets.discard(ws)
            elif pid is not None and room.sockets.get(pid) is ws:
                del room.sockets[pid]
                if pid in room.players:
                    room.players[pid].connected = False
                if room.game and pid < len(room.game.players):
                    room.game.players[pid].connected = False
                # 大厅时玩家走了 → 从座位移除（机器人座位不会走人）
                if not room.in_game() and not room.game:
                    if pid in room.players:
                        del room.players[pid]
                # 投票期间断线
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

    # ---- 机器人管理（仅大厅）----
    if t == "add_bot":
        async with room.lock:
            if room.in_game():
                await ws.send_json({"type": "error", "msg": "游戏中无法添加机器人"})
                return
            free = room.free_seats()
            if not free:
                await ws.send_json({"type": "error", "msg": "房间已满，无法添加机器人"})
                return
            seat = free[0]
            bot_name = _gen_bot_name()
            room.players[seat] = Player(seat, bot_name, is_bot=True)
            room.players[seat].connected = True  # bot 视为永远在线
        await broadcast_state_or_lobby()
        return

    if t == "kick_bot":
        # 踢出最后一个机器人；可选指定 seat
        target_seat = msg.get("seat")
        async with room.lock:
            if room.in_game():
                await ws.send_json({"type": "error", "msg": "游戏中无法踢出机器人"})
                return
            bot_seats = room.bot_seats()
            if not bot_seats:
                await ws.send_json({"type": "error", "msg": "没有机器人可以踢"})
                return
            if isinstance(target_seat, int) and target_seat in bot_seats:
                seat = target_seat
            else:
                seat = bot_seats[-1]  # 默认踢最后一个
            del room.players[seat]
        await broadcast_state_or_lobby()
        return

    if t == "kick_player":
        # 大厅踢人（任何在大厅的人都可踢任何人，包括踢自己＝退出座位）
        target = msg.get("seat")
        async with room.lock:
            if room.in_game():
                await ws.send_json({"type": "error", "msg": "游戏中不能踢人，请使用投票退出"})
                return
            if not isinstance(target, int) or target not in room.players:
                await ws.send_json({"type": "error", "msg": "目标座位无效"})
                return
            # 关掉对方的 socket（如果有）
            if target in room.sockets:
                old = room.sockets[target]
                try: await old.close()
                except: pass
                room.sockets.pop(target, None)
            del room.players[target]
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
            if room.current_round == 0:
                for p in room.players.values():
                    p.score = 0
            game_players = [room.players[i] for i in taken_idx]
            room.game = Game(game_players)
            first = room.current_round % len(game_players)
            room.game.start(first_player=first)
            _start_turn_timer()
        await broadcast_state()
        await _maybe_trigger_bot()
        return

    if t == "next_round":
        async with room.lock:
            if not room.game or not room.game.game_over:
                await ws.send_json({"type": "error", "msg": "上一局尚未结束"})
                return
            if room.current_round >= room.total_rounds:
                await ws.send_json({"type": "error", "msg": "整轮已结束"})
                return
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
        await _maybe_trigger_bot()
        return

    if t == "reset_match":
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
                if not room.exit_vote_active:
                    room.exit_vote_active = True
                    room.exit_vote_deadline = time.time() + EXIT_VOTE_TIMEOUT
                    _start_exit_vote_timer()
                room.exit_votes.add(pid)
            else:
                room.exit_votes.discard(pid)
                if not room.exit_votes:
                    _stop_exit_vote_timer()
                    room.exit_vote_active = False
                    room.exit_vote_deadline = None
            if len(room.exit_votes) >= 2:
                _stop_exit_vote_timer()
                room.game._log("🚪 投票通过：本局中止，赛制重置，所有分数清零", "event")
                room.game.game_over = True
                room.game.winner_id = None
                room.exit_votes.clear()
                room.exit_vote_active = False
                room.exit_vote_deadline = None
                _stop_turn_timer()
                for p in room.players.values():
                    p.score = 0
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


# ============= 机器人名字生成 =============
_BOT_ADJ = ["神秘的", "狡黠的", "鲁莽的", "优雅的", "暴躁的", "沉默的", "诡异的", "狂妄的"]
_BOT_NAME = ["影法师", "炼金狮", "夜行猫", "炎术士", "霜语者", "符文鸦", "幽魂猪", "雷霆熊"]


def _gen_bot_name() -> str:
    used = {p.name for p in room.players.values()}
    for _ in range(20):
        name = random.choice(_BOT_ADJ) + random.choice(_BOT_NAME)
        if len(name) > 12:
            name = name[:12]
        if name not in used:
            return name
    # 兜底
    return f"机器人{len([p for p in room.players.values() if p.is_bot]) + 1}"


# ============= 超时定时器 =============
def _stop_exit_vote_timer():
    if room.exit_vote_timer:
        room.exit_vote_timer.cancel()
        room.exit_vote_timer = None


def _start_exit_vote_timer():
    _stop_exit_vote_timer()
    deadline = room.exit_vote_deadline

    async def _wait():
        try:
            await asyncio.sleep(EXIT_VOTE_TIMEOUT)
            async with room.lock:
                if not room.exit_vote_active: return
                if room.exit_vote_deadline != deadline: return
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
    _stop_turn_timer()
    if not room.game or room.game.game_over:
        return
    # 当前 actor（当前玩家或 pending 玩家）如果是 bot，不用倒计时（bot 自动行动）
    actor_pid = room.game.current_player
    if room.game.pending is not None:
        actor_pid = room.game.pending.get("player_id", actor_pid)
    if 0 <= actor_pid < len(room.game.players) and room.game.players[actor_pid].is_bot:
        # 不启动倒计时，倒计时显示为空
        return

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
    """每次有效操作后调用：广播状态 + 重启计时器 + 触发机器人（如果该机器人行动）"""
    if room.game:
        if room.game.game_over:
            _stop_turn_timer()
            if room.game.winner_id is not None:
                room.current_round += 1
                winner = room.game.players[room.game.winner_id]
                winner.score += 1
            await broadcast_state()
        else:
            _start_turn_timer()
            await broadcast_state()
            await _maybe_trigger_bot()
    else:
        await broadcast_lobby()


async def _maybe_trigger_bot():
    """如果当前轮到机器人（或机器人有 pending），启动一个延迟任务让它行动"""
    if not room.game or room.game.game_over:
        return
    # 已有 bot 任务在跑就不启动新的
    if room.bot_task and not room.bot_task.done():
        return

    # 谁是当前 actor？
    actor_pid = room.game.current_player
    if room.game.pending is not None:
        actor_pid = room.game.pending.get("player_id", actor_pid)
    if not (0 <= actor_pid < len(room.game.players)):
        return
    if not room.game.players[actor_pid].is_bot:
        return

    room.bot_task = asyncio.create_task(_run_bot(actor_pid))


async def _run_bot(bot_pid: int):
    """机器人行动循环：连续做决策直到不该自己动 / pending 不是自己"""
    try:
        # 思考延迟（让玩家看清楚发生了什么）
        delay = random.uniform(BOT_THINK_MIN, BOT_THINK_MAX)
        await asyncio.sleep(delay)

        async with room.lock:
            if not room.game or room.game.game_over:
                return
            # 重新校验当前 actor 仍是这个 bot
            actor_pid = room.game.current_player
            if room.game.pending is not None:
                actor_pid = room.game.pending.get("player_id", actor_pid)
            if actor_pid != bot_pid:
                return
            if not room.game.players[bot_pid].is_bot:
                return

            decision = room.game.bot_decide(bot_pid)
            act = decision.get("action")
            if act == "call":
                room.game.call_number(bot_pid, decision["n"])
            elif act == "pass":
                room.game.pass_turn(bot_pid)
            elif act == "confirm_dice":
                room.game.confirm_dice(bot_pid)
            elif act == "choose_mystery_peek":
                room.game.choose_mystery_peek(bot_pid, decision["idx"])
            elif act == "confirm_peek_mystery":
                room.game.confirm_peek_mystery(bot_pid)
            else:
                return
        # 释放锁后调用 _on_action_done（它会再次广播 + 触发下一个 bot）
        await _on_action_done()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Bot {bot_pid} error: {e}")


# ============= 广播 =============
async def broadcast_state():
    if not room.game: return
    extra = _match_meta()
    # 真人玩家
    for pid, ws in list(room.sockets.items()):
        try:
            data = room.game.state_for(pid)
            data.update(extra)
            await ws.send_json({"type": "state", "data": data})
        except: pass
    # 观战者
    for ws in list(room.spectator_sockets):
        try:
            data = room.game.state_for(-1, spectator=True)
            data.update(extra)
            await ws.send_json({"type": "state", "data": data})
        except: pass


async def broadcast_lobby():
    seats = []
    for i in range(6):
        if i in room.players:
            p = room.players[i]
            seats.append({
                "seat": i, "taken": True,
                "online": (i in room.sockets) or p.is_bot,  # bot 永远在线
                "name": p.name, "score": p.score,
                "is_bot": p.is_bot,
            })
        else:
            seats.append({
                "seat": i, "taken": False, "online": False,
                "name": None, "score": 0, "is_bot": False,
            })
    base_data = {
        "occupied": room.occupied(),
        "human_count": room.human_count(),
        "seats": seats,
        "in_game": room.in_game(),
        "total_rounds": room.total_rounds,
        "current_round": room.current_round,
        "match_done": (room.current_round >= room.total_rounds and room.current_round > 0),
        "spectator_count": len(room.spectator_sockets),
    }
    # 真人玩家
    for pid, ws in list(room.sockets.items()):
        try:
            payload = {"type": "lobby", "data": dict(base_data)}
            payload["data"]["you"] = pid
            payload["data"]["is_spectator"] = False
            await ws.send_json(payload)
        except: pass
    # 观战者
    for ws in list(room.spectator_sockets):
        try:
            payload = {"type": "lobby", "data": dict(base_data)}
            payload["data"]["you"] = None
            payload["data"]["is_spectator"] = True
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
        "spectator_count": len(room.spectator_sockets),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
