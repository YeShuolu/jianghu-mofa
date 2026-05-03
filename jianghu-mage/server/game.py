"""
江湖魔法师 · 游戏核心逻辑
将原前端 JS 的游戏机制搬到 Python，方便服务器侧权威判定
"""
import random
from typing import Optional

CARD_DEFS = {
    1: {"name": "红龙召唤", "icon": "🐉", "desc": "喊对：除自己外每人扣2血。喊错：自己扣2血"},
    2: {"name": "恶灵附身", "icon": "👻", "desc": "其他人各扣1血，自己回1血"},
    3: {"name": "治愈术",   "icon": "🌈", "desc": "掷骰，回复1-6点生命"},
    4: {"name": "窥视",     "icon": "🦉", "desc": "查看自己的1张手牌"},
    5: {"name": "闪电风暴", "icon": "⚡", "desc": "左右两边玩家各扣1血"},
    6: {"name": "火球术",   "icon": "🔥", "desc": "左边玩家扣1血"},
    7: {"name": "冰锥术",   "icon": "❄️", "desc": "右边玩家扣1血"},
    8: {"name": "治疗药剂", "icon": "🧪", "desc": "自己回1血"},
}

MAX_HP = 6
INITIAL_HAND = 5


class Player:
    def __init__(self, pid: int, name: str):
        self.id = pid
        self.name = name
        self.hp = MAX_HP
        self.hand: list[int] = []
        self.eliminated = False
        self.peeked_idx: Optional[int] = None
        self.connected = False

    def to_public(self):
        """对其他玩家可见的视图（包含手牌内容，因为别人能看到你的牌）"""
        return {
            "id": self.id,
            "name": self.name,
            "hp": self.hp,
            "hand": self.hand,
            "hand_size": len(self.hand),
            "eliminated": self.eliminated,
            "connected": self.connected,
        }

    def to_self(self):
        """对自己可见的视图（手牌只暴露已 peek 的那张）"""
        return {
            "id": self.id,
            "name": self.name,
            "hp": self.hp,
            "hand_size": len(self.hand),
            "peeked_idx": self.peeked_idx,
            "peeked_card": self.hand[self.peeked_idx] if self.peeked_idx is not None and self.peeked_idx < len(self.hand) else None,
            "eliminated": self.eliminated,
            "connected": self.connected,
        }


class Game:
    """单局游戏状态机。所有规则判定都在这里。"""

    def __init__(self, player_count: int = 3):
        assert 3 <= player_count <= 6
        self.player_count = player_count
        self.players: list[Player] = [
            Player(i, f"玩家{chr(ord('A') + i)}") for i in range(player_count)
        ]
        self.deck: list[int] = []
        self.discard_counts: dict[int, int] = {n: 0 for n in range(1, 9)}
        self.current_player: int = 0
        self.min_callable: int = 1
        self.has_called_this_turn: bool = False
        self.game_over: bool = False
        self.winner_id: Optional[int] = None
        self.log: list[dict] = []
        self.started: bool = False
        # 待玩家决策的窥视/掷骰结果，由前端确认后再继续
        self.pending: Optional[dict] = None

    # ============= 初始化 =============
    def start(self):
        deck = []
        for n in range(1, 9):
            deck.extend([n] * n)
        random.shuffle(deck)
        for p in self.players:
            p.hand = [deck.pop() for _ in range(INITIAL_HAND)]
            p.hp = MAX_HP
            p.eliminated = False
            p.peeked_idx = None
        self.deck = deck
        self.current_player = 0
        self.min_callable = 1
        self.has_called_this_turn = False
        self.game_over = False
        self.winner_id = None
        self.discard_counts = {n: 0 for n in range(1, 9)}
        self.pending = None
        self.log = []
        self.started = True
        self._log(f"🎴 游戏开始！{self.player_count} 人局，每人 {MAX_HP} 滴血、{INITIAL_HAND} 张手牌", "event")
        self._log(f"— 轮到 {self.players[0].name} 出牌 —", "")

    # ============= 工具 =============
    def _log(self, text: str, cls: str = ""):
        self.log.append({"text": text, "cls": cls})
        if len(self.log) > 100:
            self.log = self.log[-100:]

    def _alive(self) -> list[Player]:
        return [p for p in self.players if not p.eliminated]

    def _left_player(self, pid: int) -> Optional[Player]:
        """逆时针上家（跳过死人）"""
        n = len(self.players)
        for i in range(1, n):
            idx = (pid - i) % n
            if not self.players[idx].eliminated:
                return self.players[idx]
        return None

    def _right_player(self, pid: int) -> Optional[Player]:
        """顺时针下家（跳过死人）"""
        n = len(self.players)
        for i in range(1, n):
            idx = (pid + i) % n
            if not self.players[idx].eliminated:
                return self.players[idx]
        return None

    def _next_player(self, pid: int) -> Optional[Player]:
        return self._right_player(pid)

    def _damage(self, p: Player, amount: int):
        p.hp = max(0, p.hp - amount)
        if p.hp == 0 and not p.eliminated:
            p.eliminated = True
            self._log(f"💀 {p.name} 血量归零，出局！", "damage")

    def _heal(self, p: Player, amount: int):
        p.hp = min(MAX_HP, p.hp + amount)

    # ============= 主行动：喊牌 =============
    def call_number(self, pid: int, n: int) -> dict:
        """
        玩家喊一个号码。
        返回事件列表给前端做动画提示。
        """
        if self.game_over:
            return {"ok": False, "reason": "game_over"}
        if self.pending is not None:
            return {"ok": False, "reason": "pending_action", "pending": self.pending}
        if self.current_player != pid:
            return {"ok": False, "reason": "not_your_turn"}
        if not (1 <= n <= 8):
            return {"ok": False, "reason": "invalid_number"}
        if n < self.min_callable:
            return {"ok": False, "reason": "below_min_callable"}

        player = self.players[pid]
        self._log(f"▶ {player.name} 喊：{n}号 · {CARD_DEFS[n]['name']}", "")

        # 优先打出被 peek 揭示的那张
        idx = -1
        if player.peeked_idx is not None and player.peeked_idx < len(player.hand) and player.hand[player.peeked_idx] == n:
            idx = player.peeked_idx
        elif n in player.hand:
            idx = player.hand.index(n)

        if idx == -1:
            # 喊错
            penalty = 2 if n == 1 else 1
            if n == 1:
                self._log(f"✗ {player.name} 手里没有 1 号牌！红龙反噬，扣 2 血", "damage")
            else:
                self._log(f"✗ {player.name} 手里没有 {n} 号牌！扣 1 血", "damage")
            self._damage(player, penalty)
            self._end_turn()
            return {"ok": True, "outcome": "miss"}

        # 喊对
        player.hand.pop(idx)
        self.discard_counts[n] += 1
        if player.peeked_idx is not None:
            if player.peeked_idx == idx:
                player.peeked_idx = None
            elif player.peeked_idx > idx:
                player.peeked_idx -= 1
        self._log(f"✓ {player.name} 打出：{n}号 · {CARD_DEFS[n]['name']} {CARD_DEFS[n]['icon']}", "event")
        self.min_callable = n
        self.has_called_this_turn = True
        self._cast_spell(player, n)

        # 法术结束后判定胜利
        if not self.game_over and len(player.hand) == 0:
            self._log(f"🏆 {player.name} 一回合内打光手牌，获得胜利！", "win")
            self.game_over = True
            self.winner_id = player.id
        if not self.game_over:
            self._check_win_by_elim()

        return {"ok": True, "outcome": "hit"}

    # ============= 过牌 =============
    def pass_turn(self, pid: int) -> dict:
        if self.game_over:
            return {"ok": False, "reason": "game_over"}
        if self.pending is not None:
            return {"ok": False, "reason": "pending_action"}
        if self.current_player != pid:
            return {"ok": False, "reason": "not_your_turn"}
        if not self.has_called_this_turn:
            return {"ok": False, "reason": "must_call_at_least_once"}
        self._log(f"⏭ {self.players[pid].name} 主动过牌", "")
        self._end_turn()
        return {"ok": True}

    # ============= 法术 =============
    def _cast_spell(self, player: Player, n: int):
        if n == 1:
            self._log("🐉 红龙降临，对所有敌人造成 2 点伤害", "damage")
            for p in self._alive():
                if p.id != player.id:
                    self._damage(p, 2)
        elif n == 2:
            self._log(f"👻 恶灵附身：其他人各扣 1 血，{player.name} 回 1 血", "event")
            for p in self._alive():
                if p.id != player.id:
                    self._damage(p, 1)
            self._heal(player, 1)
        elif n == 3:
            roll = random.randint(1, 6)
            self._log(f"🎲 {player.name} 掷出 {roll}，回复 {roll} 点生命", "heal")
            self._heal(player, roll)
            self.pending = {"kind": "dice", "player_id": player.id, "value": roll}
        elif n == 4:
            if len(player.hand) > 0:
                self.pending = {"kind": "peek_choose", "player_id": player.id, "hand_size": len(player.hand)}
        elif n == 5:
            self._log("⚡ 闪电风暴击中两侧玩家", "damage")
            targets = set()
            L = self._left_player(player.id)
            R = self._right_player(player.id)
            if L: targets.add(L.id)
            if R: targets.add(R.id)
            for tid in targets:
                self._damage(self.players[tid], 1)
        elif n == 6:
            L = self._left_player(player.id)
            if L:
                self._log(f"🔥 火球飞向左边的 {L.name}", "damage")
                self._damage(L, 1)
        elif n == 7:
            R = self._right_player(player.id)
            if R:
                self._log(f"❄️ 冰锥刺向右边的 {R.name}", "damage")
                self._damage(R, 1)
        elif n == 8:
            self._log(f"🧪 {player.name} 喝下药剂，回 1 血", "heal")
            self._heal(player, 1)

    # ============= 处理 pending（玩家确认弹窗） =============
    def confirm_dice(self, pid: int) -> dict:
        if not self.pending or self.pending["kind"] != "dice" or self.pending["player_id"] != pid:
            return {"ok": False}
        self.pending = None
        return {"ok": True}

    def choose_peek(self, pid: int, idx: int) -> dict:
        if not self.pending or self.pending["kind"] != "peek_choose" or self.pending["player_id"] != pid:
            return {"ok": False}
        player = self.players[pid]
        if not (0 <= idx < len(player.hand)):
            return {"ok": False, "reason": "invalid_idx"}
        player.peeked_idx = idx
        cnum = player.hand[idx]
        self._log(f"🦉 {player.name} 窥视了一张手牌（自己看到）", "event")
        # 让玩家先确认看到什么，再继续
        self.pending = {"kind": "peek_show", "player_id": pid, "idx": idx, "card": cnum}
        return {"ok": True, "card": cnum}

    def confirm_peek(self, pid: int) -> dict:
        if not self.pending or self.pending["kind"] != "peek_show" or self.pending["player_id"] != pid:
            return {"ok": False}
        self.pending = None
        return {"ok": True}

    # ============= 回合结束 / 胜负 =============
    def _end_turn(self):
        cur = self.players[self.current_player]
        if not cur.eliminated:
            drew = 0
            while len(cur.hand) < INITIAL_HAND and len(self.deck) > 0:
                cur.hand.append(self.deck.pop())
                drew += 1
            if drew > 0:
                self._log(f"🃏 {cur.name} 补 {drew} 张牌（手牌 {len(cur.hand)}/{INITIAL_HAND}）", "")
        self._check_win_by_elim()
        if self.game_over:
            return
        nxt = self._next_player(self.current_player)
        if nxt:
            self.current_player = nxt.id
            self.min_callable = 1
            self.has_called_this_turn = False
            self._log(f"— 轮到 {nxt.name} 出牌 —", "")

    def _check_win_by_elim(self):
        alive = self._alive()
        if len(alive) <= 1:
            self.game_over = True
            if alive:
                self.winner_id = alive[0].id
                self._log(f"🏆 {alive[0].name} 是最后存活者，游戏结束！", "win")
            else:
                self._log("平局，游戏结束", "win")

    # ============= 状态序列化（按视角） =============
    def state_for(self, viewer_id: int) -> dict:
        """以 viewer 的视角返回游戏状态（隐藏自己的手牌）"""
        # 游戏结束后揭示所有手牌
        reveal_all = self.game_over
        return {
            "started": self.started,
            "player_count": self.player_count,
            "current_player": self.current_player,
            "min_callable": self.min_callable,
            "has_called_this_turn": self.has_called_this_turn,
            "discard_counts": self.discard_counts,
            "deck_size": len(self.deck),
            "game_over": self.game_over,
            "winner_id": self.winner_id,
            "log": self.log[-60:],
            "viewer_id": viewer_id,
            "pending": self.pending if self.pending and self.pending.get("player_id") == viewer_id else (
                {"kind": self.pending["kind"], "player_id": self.pending["player_id"]} if self.pending else None
            ),
            "players": [
                (p.to_self() if p.id == viewer_id and not reveal_all else p.to_public())
                for p in self.players
            ],
        }
