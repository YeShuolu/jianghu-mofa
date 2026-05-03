"""
江湖魔法师 · FastAPI 服务器
单房间 + 匿名进入 + WebSocket 实时同步
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from game import Game

# ============= 房间状态（单房间，全局变量即可） =============
class Room:
    def __init__(self):
        self.game: Optional[Game] = None
        self.sockets: dict[int, WebSocket] = {}  # pid -> ws
        self.seat_taken: list[bool] = [False] * 6  # 最多 6 个座位
        self.player_count: int = 0  # 实际开局人数（开始时确定）
        self.lock = asyncio.Lock()

    def free_seats(self) -> list[int]:
        return [i for i, taken in enumerate(self.seat_taken) if not taken]

    def occupied(self) -> int:
        return sum(self.seat_taken)


room = Room()


# ============= FastAPI 应用 =============
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🎮 江湖魔法师服务器启动")
    yield
    print("服务器关闭")


app = FastAPI(lifespan=lifespan)


# ============= 静态文件 =============
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# 挂载 static 路径供未来扩展
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ============= 房间状态查询（HTTP） =============
@app.get("/api/room")
async def room_status():
    return {
        "occupied": room.occupied(),
        "max_seats": 6,
        "started": room.game.started if room.game else False,
        "free_seats": room.free_seats(),
    }


@app.post("/api/reset")
async def reset_room():
    """重置房间（管理员用，无密码 — 仅适合朋友局）"""
    async with room.lock:
        room.game = None
        room.seat_taken = [False] * 6
        room.player_count = 0
        # 主动断开所有 ws
        for ws in list(room.sockets.values()):
            try:
                await ws.close()
            except:
                pass
        room.sockets.clear()
    return {"ok": True}


# ============= WebSocket =============
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, seat: Optional[int] = Query(None)):
    """
    座位号约定：客户端通过 ?seat=N 指定要坐哪个位置；
      - 如果 seat 已被占用 → 拒绝
      - 如果 seat 为 None → 自动分配第一个空位
      - 如果游戏已开始 → 仅允许已分配座位的玩家重连
    """
    await ws.accept()
    pid: Optional[int] = None
    try:
        async with room.lock:
            if seat is not None:
                if not (0 <= seat < 6):
                    await ws.send_json({"type": "error", "msg": "座位号无效"})
                    await ws.close()
                    return
                if room.seat_taken[seat] and seat in room.sockets:
                    # 已有人在该座位且 socket 还在 → 拒绝重复占座
                    await ws.send_json({"type": "error", "msg": f"座位 {seat+1} 已被占用"})
                    await ws.close()
                    return
                pid = seat
            else:
                if room.game and room.game.started:
                    await ws.send_json({"type": "error", "msg": "游戏已开始，无法加入新玩家"})
                    await ws.close()
                    return
                free = room.free_seats()
                if not free:
                    await ws.send_json({"type": "error", "msg": "房间已满"})
                    await ws.close()
                    return
                pid = free[0]

            # 占座
            room.seat_taken[pid] = True
            # 关闭旧的 socket
            old = room.sockets.get(pid)
            if old:
                try: await old.close()
                except: pass
            room.sockets[pid] = ws
            if room.game:
                room.game.players[pid].connected = True

        await ws.send_json({"type": "joined", "pid": pid})
        await broadcast_lobby_or_state()

        # 主消息循环
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
                if room.game:
                    room.game.players[pid].connected = False
                else:
                    # 还没开始游戏 → 释放座位
                    room.seat_taken[pid] = False
        await broadcast_lobby_or_state()


# ============= 消息分发 =============
async def handle_message(pid: int, msg: dict, ws: WebSocket):
    t = msg.get("type")

    if t == "set_name":
        # 改名（可选；前端用 prompt 让用户改）
        name = (msg.get("name") or "").strip()[:12]
        if not name:
            return
        async with room.lock:
            if room.game:
                room.game.players[pid].name = name
        await broadcast_lobby_or_state()
        return

    if t == "start_game":
        # 任何已入座的玩家都可发起开始（朋友局，简单）
        async with room.lock:
            occ = room.occupied()
            if occ < 3:
                await ws.send_json({"type": "error", "msg": f"至少需要 3 人才能开始（当前 {occ} 人）"})
                return
            if room.game and room.game.started and not room.game.game_over:
                await ws.send_json({"type": "error", "msg": "本局尚未结束"})
                return
            # 按已占座位的顺序压缩为连续 pid（保留座位号即玩家身份）
            # 简化：直接以 6 个座位中已占用的为玩家，但 game 内 pid 与 seat 保持一致
            # 因此 game.player_count = max(occupied seat index)+1 但内部 player[i].eliminated 不该有效... 
            # 改为 game 接受 player_count 等于占用座位数 — 但要求座位是 0..N-1 连续！
            # 朋友局简化：要求座位连续。如果不连续，提示。
            taken_idx = [i for i, t in enumerate(room.seat_taken) if t]
            if taken_idx != list(range(len(taken_idx))):
                await ws.send_json({"type": "error", "msg": f"座位不连续（已占：{[i+1 for i in taken_idx]}），请确保从 1 号位起依次落座"})
                return
            n = len(taken_idx)
            # 保留旧名字
            old_names = {}
            if room.game:
                for p in room.game.players:
                    old_names[p.id] = p.name
            room.game = Game(player_count=n)
            for p in room.game.players:
                if p.id in old_names:
                    p.name = old_names[p.id]
                p.connected = (p.id in room.sockets)
            room.player_count = n
            room.game.start()
        await broadcast_state()
        return

    # 以下需要游戏开始
    if not room.game or not room.game.started:
        await ws.send_json({"type": "error", "msg": "游戏未开始"})
        return

    if t == "call":
        n = msg.get("n")
        if not isinstance(n, int):
            return
        async with room.lock:
            res = room.game.call_number(pid, n)
        if not res.get("ok"):
            await ws.send_json({"type": "error", "msg": _reason_to_text(res.get("reason"))})
            return
        await broadcast_state()
        return

    if t == "pass":
        async with room.lock:
            res = room.game.pass_turn(pid)
        if not res.get("ok"):
            await ws.send_json({"type": "error", "msg": _reason_to_text(res.get("reason"))})
            return
        await broadcast_state()
        return

    if t == "confirm_dice":
        async with room.lock:
            room.game.confirm_dice(pid)
        await broadcast_state()
        return

    if t == "choose_peek":
        idx = msg.get("idx")
        if not isinstance(idx, int):
            return
        async with room.lock:
            room.game.choose_peek(pid, idx)
        await broadcast_state()
        return

    if t == "confirm_peek":
        async with room.lock:
            room.game.confirm_peek(pid)
        await broadcast_state()
        return


def _reason_to_text(reason: str) -> str:
    return {
        "game_over": "游戏已结束",
        "pending_action": "请先完成当前动作",
        "not_your_turn": "还没轮到你",
        "invalid_number": "无效号码",
        "below_min_callable": "号码必须 ≥ 当前最小可喊号",
        "must_call_at_least_once": "本回合必须先成功喊出至少一张牌才能过牌",
    }.get(reason or "", reason or "未知错误")


# ============= 广播 =============
async def broadcast_state():
    """游戏中：给每个玩家发自己视角的 state"""
    if not room.game:
        return
    for pid, ws in list(room.sockets.items()):
        try:
            payload = {"type": "state", "data": room.game.state_for(pid)}
            await ws.send_json(payload)
        except Exception:
            pass


async def broadcast_lobby():
    """大厅状态（未开始或被重置）"""
    lobby = {
        "type": "lobby",
        "data": {
            "occupied": room.occupied(),
            "seats": [
                {"seat": i, "taken": room.seat_taken[i], "online": i in room.sockets,
                 "name": (room.game.players[i].name if room.game and i < len(room.game.players) else None)}
                for i in range(6)
            ],
            "started": room.game.started if room.game else False,
            "game_over": room.game.game_over if room.game else False,
        }
    }
    for pid, ws in list(room.sockets.items()):
        try:
            payload = dict(lobby)
            payload["data"] = dict(lobby["data"])
            payload["data"]["you"] = pid
            await ws.send_json(payload)
        except Exception:
            pass


async def broadcast_lobby_or_state():
    if room.game and room.game.started and not room.game.game_over:
        await broadcast_state()
    else:
        await broadcast_lobby()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
