"""
江湖魔法师 · 游戏核心逻辑 v3
v3 新增：
- 机器人玩家（is_bot 字段 + bot_decide AI 决策）
- 死亡后玩家立即可见自己手牌
- 观战支持（state_for 接受 -1 表示观战者，看到所有信息）
"""
import random
from typing import Optional

CARD_DEFS = {
    1: {"name": "红龙召唤", "icon": "🐉", "desc": "喊对：除自己外每人随机扣1-3血。喊错：自己随机扣1-3血"},
    2: {"name": "恶灵附身", "icon": "👻", "desc": "其他人各扣1血，自己回1血"},
    3: {"name": "治愈术",   "icon": "🌈", "desc": "掷骰，回复1-3点生命"},
    4: {"name": "窥视",     "icon": "🦉", "desc": "查看奥秘堆中一张未揭示的牌"},
    5: {"name": "闪电风暴", "icon": "⚡", "desc": "左右两边玩家各扣1血"},
    6: {"name": "火球术",   "icon": "🔥", "desc": "左边玩家扣1血"},
    7: {"name": "冰锥术",   "icon": "❄️", "desc": "右边玩家扣1血"},
    8: {"name": "治疗药剂", "icon": "🧪", "desc": "自己回1血"},
}

MAX_HP = 6
INITIAL_HAND = 5
MYSTERY_DECK_SIZE = 4
FIRST_DECISION_SECONDS = 45
SUBSEQUENT_DECISION_SECONDS = 30


class Player:
    def __init__(self, pid: int, name: str, is_bot: bool = False):
        self.id = pid
        self.name = name
        self.hp = MAX_HP
        self.hand: list[int] = []
        self.eliminated = False
        self.peeked_mysteries: dict[int, int] = {}
        self.connected = False
        self.score = 0
        self.is_bot = is_bot

    def to_public(self):
        return {
            "id": self.id, "name": self.name, "hp": self.hp,
            "hand": self.hand, "hand_size": len(self.hand),
            "eliminated": self.eliminated, "connected": self.connected,
            "score": self.score, "is_bot": self.is_bot,
        }

    def to_self(self):
        return {
            "id": self.id, "name": self.name, "hp": self.hp,
            "hand_size": len(self.hand),
            "eliminated": self.eliminated, "connected": self.connected,
            "score": self.score, "is_bot": self.is_bot,
            "peeked_mysteries": self.peeked_mysteries,
        }

    def to_dead_self(self):
        """已出局的玩家也能看到自己的手牌（虽然手牌已经不会再用了，但有助于复盘）"""
        d = self.to_public()
        d["peeked_mysteries"] = self.peeked_mysteries
        return d


class Game:
    def __init__(self, players: list[Player]):
        self.players = players
        self.player_count = len(players)
        self.deck: list[int] = []
        self.mystery_deck: list[int] = []
        self.mystery_revealed: list[bool] = []
        self.discard_counts: dict[int, int] = {n: 0 for n in range(1, 9)}
        self.current_player: int = 0
        self.min_callable: int = 1
        self.has_called_this_turn: bool = False
        self.game_over: bool = False
        self.winner_id: Optional[int] = None
        self.log: list[dict] = []
        self.started: bool = False
        self.pending: Optional[dict] = None
        self.turn_deadline: Optional[float] = None

    def start(self, first_player: int = 0):
        deck = []
        for n in range(1, 9):
            deck.extend([n] * n)
        random.shuffle(deck)
        self.mystery_deck = [deck.pop() for _ in range(MYSTERY_DECK_SIZE)]
        self.mystery_revealed = [False] * MYSTERY_DECK_SIZE
        for p in self.players:
            p.hand = [deck.pop() for _ in range(INITIAL_HAND)]
            p.hp = MAX_HP
            p.eliminated = False
            p.peeked_mysteries = {}
        self.deck = deck
        self.current_player = first_player % self.player_count
        while self.players[self.current_player].eliminated:
            self.current_player = (self.current_player + 1) % self.player_count
        self.min_callable = 1
        self.has_called_this_turn = False
        self.game_over = False
        self.winner_id = None
        self.discard_counts = {n: 0 for n in range(1, 9)}
        self.pending = None
        self.log = []
        self.started = True
        self._log(f"🎴 新一局开始！{self.player_count} 人局，每人 {MAX_HP} 滴血、{INITIAL_HAND} 张手牌", "event")
        self._log(f"🔮 奥秘牌堆已布置 {MYSTERY_DECK_SIZE} 张暗牌（用 4 号牌窥视可获取）", "event")
        self._log(f"— 轮到 {self.players[self.current_player].name} 出牌 —", "")

    def _log(self, text, cls=""):
        self.log.append({"text": text, "cls": cls})
        if len(self.log) > 100: self.log = self.log[-100:]

    def _alive(self): return [p for p in self.players if not p.eliminated]

    def _left_player(self, pid):
        n = len(self.players)
        for i in range(1, n):
            idx = (pid - i) % n
            if not self.players[idx].eliminated: return self.players[idx]
        return None

    def _right_player(self, pid):
        n = len(self.players)
        for i in range(1, n):
            idx = (pid + i) % n
            if not self.players[idx].eliminated: return self.players[idx]
        return None

    def _next_player(self, pid): return self._right_player(pid)

    def _damage(self, p, amount):
        p.hp = max(0, p.hp - amount)
        if p.hp == 0 and not p.eliminated:
            p.eliminated = True
            self._log(f"💀 {p.name} 血量归零，出局！", "damage")

    def _heal(self, p, amount):
        p.hp = min(MAX_HP, p.hp + amount)

    def call_number(self, pid: int, n: int) -> dict:
        if self.game_over: return {"ok": False, "reason": "game_over"}
        if self.pending is not None: return {"ok": False, "reason": "pending_action"}
        if self.current_player != pid: return {"ok": False, "reason": "not_your_turn"}
        if not (1 <= n <= 8): return {"ok": False, "reason": "invalid_number"}
        if n < self.min_callable: return {"ok": False, "reason": "below_min_callable"}

        player = self.players[pid]
        self._log(f"▶ {player.name} 喊：{n}号 · {CARD_DEFS[n]['name']}", "")

        idx = player.hand.index(n) if n in player.hand else -1
        if idx == -1:
            if n == 1:
                penalty = random.randint(1, 3)
                self._log(f"✗ {player.name} 手里没有 1 号牌！红龙反噬，随机扣 {penalty} 血", "damage")
            else:
                penalty = 1
                self._log(f"✗ {player.name} 手里没有 {n} 号牌！扣 1 血", "damage")
            self._damage(player, penalty)
            self._end_turn()
            return {"ok": True, "outcome": "miss"}

        player.hand.pop(idx)
        self.discard_counts[n] += 1
        self._log(f"✓ {player.name} 打出：{n}号 · {CARD_DEFS[n]['name']} {CARD_DEFS[n]['icon']}", "event")
        self.min_callable = n
        self.has_called_this_turn = True
        self._cast_spell(player, n)

        if not self.game_over and len(player.hand) == 0:
            self._log(f"🏆 {player.name} 一回合内打光手牌，获得本局胜利！", "win")
            self.game_over = True
            self.winner_id = player.id
        if not self.game_over:
            self._check_win_by_elim()
        return {"ok": True, "outcome": "hit"}

    def pass_turn(self, pid: int) -> dict:
        if self.game_over: return {"ok": False, "reason": "game_over"}
        if self.pending is not None: return {"ok": False, "reason": "pending_action"}
        if self.current_player != pid: return {"ok": False, "reason": "not_your_turn"}
        if not self.has_called_this_turn: return {"ok": False, "reason": "must_call_at_least_once"}
        self._log(f"⏭ {self.players[pid].name} 主动过牌", "")
        self._end_turn()
        return {"ok": True}

    def timeout_current(self) -> dict:
        if self.game_over or self.pending is not None: return {"ok": False}
        cur = self.players[self.current_player]
        if cur.eliminated: return {"ok": False}
        self._log(f"⏰ {cur.name} 思考超时，扣 1 血", "damage")
        self._damage(cur, 1)
        self._end_turn()
        return {"ok": True, "timed_out_pid": cur.id}

    def _cast_spell(self, player, n):
        if n == 1:
            self._log("🐉 红龙降临，对所有敌人各自造成随机伤害", "damage")
            for p in self._alive():
                if p.id != player.id:
                    dmg = random.randint(1, 3)
                    self._log(f"   → {p.name} 被烧伤 {dmg} 点", "damage")
                    self._damage(p, dmg)
        elif n == 2:
            self._log(f"👻 恶灵附身：其他人各扣 1 血，{player.name} 回 1 血", "event")
            for p in self._alive():
                if p.id != player.id: self._damage(p, 1)
            self._heal(player, 1)
        elif n == 3:
            roll = random.randint(1, 3)
            self._log(f"🎲 {player.name} 掷出 {roll}（1-3），回复 {roll} 点生命", "heal")
            self._heal(player, roll)
            self.pending = {"kind": "dice", "player_id": player.id, "value": roll}
        elif n == 4:
            unrevealed = [i for i, r in enumerate(self.mystery_revealed) if not r]
            if not unrevealed:
                self._log(f"🦉 {player.name} 想窥视奥秘堆，但所有奥秘牌都已被揭示过", "event")
            else:
                self.pending = {"kind": "peek_mystery_choose", "player_id": player.id, "available": unrevealed}
        elif n == 5:
            self._log("⚡ 闪电风暴击中两侧玩家", "damage")
            targets = set()
            L = self._left_player(player.id); R = self._right_player(player.id)
            if L: targets.add(L.id)
            if R: targets.add(R.id)
            for tid in targets: self._damage(self.players[tid], 1)
        elif n == 6:
            L = self._left_player(player.id)
            if L: self._log(f"🔥 火球飞向左边的 {L.name}", "damage"); self._damage(L, 1)
        elif n == 7:
            R = self._right_player(player.id)
            if R: self._log(f"❄️ 冰锥刺向右边的 {R.name}", "damage"); self._damage(R, 1)
        elif n == 8:
            self._log(f"🧪 {player.name} 喝下药剂，回 1 血", "heal")
            self._heal(player, 1)

    def confirm_dice(self, pid):
        if not self.pending or self.pending["kind"] != "dice" or self.pending["player_id"] != pid: return {"ok": False}
        self.pending = None
        return {"ok": True}

    def choose_mystery_peek(self, pid, mystery_idx):
        if not self.pending or self.pending["kind"] != "peek_mystery_choose" or self.pending["player_id"] != pid:
            return {"ok": False}
        if mystery_idx not in self.pending["available"]:
            return {"ok": False, "reason": "already_revealed"}
        self.mystery_revealed[mystery_idx] = True
        cnum = self.mystery_deck[mystery_idx]
        player = self.players[pid]
        player.peeked_mysteries[mystery_idx] = cnum
        self._log(f"🦉 {player.name} 揭示了奥秘堆第 {mystery_idx + 1} 张（仅本人可见内容）", "event")
        self.pending = {"kind": "peek_mystery_show", "player_id": pid, "idx": mystery_idx, "card": cnum}
        return {"ok": True, "card": cnum}

    def confirm_peek_mystery(self, pid):
        if not self.pending or self.pending["kind"] != "peek_mystery_show" or self.pending["player_id"] != pid:
            return {"ok": False}
        self.pending = None
        return {"ok": True}

    def _end_turn(self):
        cur = self.players[self.current_player]
        if not cur.eliminated:
            drew = 0
            while len(cur.hand) < INITIAL_HAND and len(self.deck) > 0:
                cur.hand.append(self.deck.pop()); drew += 1
            if drew > 0:
                self._log(f"🃏 {cur.name} 补 {drew} 张牌（手牌 {len(cur.hand)}/{INITIAL_HAND}）", "")
        self._check_win_by_elim()
        if self.game_over: return
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
                self._log(f"🏆 {alive[0].name} 是最后存活者，本局胜利！", "win")
            else:
                self._log("平局，本局结束", "win")

    # ============ AI 决策 ============
    def bot_decide(self, pid: int) -> dict:
        """
        给指定 AI 玩家做一个决策，返回 {action, ...}
        - action="call", n=N
        - action="pass"
        - action="confirm_dice"
        - action="choose_mystery_peek", idx=I
        - action="confirm_peek_mystery"
        - action="none" 表示这个时机不该让 bot 行动
        """
        if self.game_over: return {"action": "none"}

        # 处理 pending（如果是这个 bot 自己的 pending）
        if self.pending and self.pending.get("player_id") == pid:
            kind = self.pending["kind"]
            if kind == "dice":
                return {"action": "confirm_dice"}
            if kind == "peek_mystery_show":
                return {"action": "confirm_peek_mystery"}
            if kind == "peek_mystery_choose":
                avail = self.pending.get("available", [])
                if avail:
                    return {"action": "choose_mystery_peek", "idx": random.choice(avail)}
                return {"action": "confirm_peek_mystery"}
            return {"action": "none"}

        if self.current_player != pid: return {"action": "none"}
        if self.pending is not None: return {"action": "none"}

        bot = self.players[pid]
        if bot.eliminated: return {"action": "none"}

        # 真正的喊牌决策
        return self._bot_choose_call(bot)

    def _bot_choose_call(self, bot: Player) -> dict:
        """
        AI 喊牌策略：
        - bot 看不到自己手牌（公平），但能看到：所有人的牌、弃牌池、自己已揭示的奥秘牌
        - 用贝叶斯式概率估计每个号码 n 在自己手里的张数
        - 优先选预期手里有的、且能造成有利局面的号码

        策略层级（从高到低）：
        1. 如果手里只剩 1 张，且某号码估计概率最高，直接全力喊（求胜）
        2. 如果当前已喊对过 (has_called_this_turn)，并且剩下号码概率都不高，倾向过牌
        3. 否则在 [min_callable, 8] 范围里选概率最高的号码
        """
        n = len(self.players)
        # ---- 估计自己手里每个号码的张数（期望值）----
        # 总计每号码张数 = n（牌 n 共 n 张）
        # 减去：所有人公开手牌中的、弃牌池中的、自己已揭示的奥秘堆中的
        # 剩余牌池（含我手牌+其他暗牌堆+其他奥秘堆）的均匀概率分布
        my_hand_size = len(bot.hand)
        if my_hand_size == 0:
            # 手牌空了应该已经游戏结束，这里防御性返回
            return {"action": "pass"} if self.has_called_this_turn else {"action": "call", "n": self.min_callable}

        unknown_counts = {k: k for k in range(1, 9)}
        for p in self.players:
            if p.id == bot.id:
                continue  # 自己手牌看不到，所以未知
            for c in p.hand:
                unknown_counts[c] -= 1
        for k, v in self.discard_counts.items():
            unknown_counts[k] -= v
        # 自己揭示过的奥秘牌也是已知的
        for idx, c in bot.peeked_mysteries.items():
            unknown_counts[c] -= 1

        # 未知池总数
        total_unknown = sum(unknown_counts.values())
        # 未知池规模 = 我的手牌 + 主牌堆剩余 + 未被自己揭示的奥秘牌
        unrevealed_mystery = sum(1 for i, r in enumerate(self.mystery_revealed) if not r and i not in bot.peeked_mysteries)
        unknown_pool_size = my_hand_size + len(self.deck) + unrevealed_mystery

        # 防御：如果不一致（极少见，比如 unknown_counts 算出负数），用 max(0, ...)
        for k in unknown_counts:
            if unknown_counts[k] < 0:
                unknown_counts[k] = 0
        if total_unknown <= 0 or unknown_pool_size <= 0:
            # 信息匮乏，随机喊一个 ≥ min_callable
            choices = [k for k in range(self.min_callable, 9)]
            return {"action": "pass"} if (self.has_called_this_turn and random.random() < 0.5) else {"action": "call", "n": random.choice(choices)}

        # 我手牌中号码 n 的期望张数 = unknown_counts[n] * (my_hand_size / unknown_pool_size)
        ratio = my_hand_size / unknown_pool_size
        expected_in_hand = {k: unknown_counts[k] * ratio for k in range(1, 9)}

        # 候选号码 ≥ min_callable
        candidates = [k for k in range(self.min_callable, 9)]
        if not candidates:
            return {"action": "pass"} if self.has_called_this_turn else {"action": "call", "n": 1}

        # 取期望张数最高的
        candidates.sort(key=lambda k: expected_in_hand[k], reverse=True)
        best = candidates[0]
        best_p = expected_in_hand[best]

        # ---- 决定是喊还是过 ----
        # 已喊对过至少一次：如果最佳候选概率太低，过牌保命
        # 概率阈值：经验值 0.5（即期望手里至少有 0.5 张）
        if self.has_called_this_turn:
            # 受伤越重越倾向保守
            hp_factor = 1.0 - (bot.hp / MAX_HP) * 0.3  # hp=6 时 factor=0.7，hp=1 时 factor=0.95
            threshold = 0.5 * hp_factor
            if best_p < threshold:
                return {"action": "pass"}

        # 手牌只有 1 张时倾向激进喊（有一定概率赢）
        # 否则正常喊期望最高的
        # 加一点随机性避免完全可预测：在前 2 个最佳里随机
        if len(candidates) >= 2:
            top2 = candidates[:2]
            top2_p = [expected_in_hand[k] for k in top2]
            # 如果第 2 名也不差（≥ 80% 第 1 名），偶尔选第 2
            if top2_p[1] >= top2_p[0] * 0.8 and random.random() < 0.3:
                return {"action": "call", "n": top2[1]}

        return {"action": "call", "n": best}

    def state_for(self, viewer_id: int, spectator: bool = False) -> dict:
        """
        viewer_id: -1 表示观战者（spectator=True 时无效，取所有信息）
        spectator: 如果 True，不论 viewer_id 是什么，都按"观战者"返回（看到所有手牌+所有奥秘）
        """
        reveal_all = self.game_over or spectator
        viewer = self.players[viewer_id] if (not spectator) and 0 <= viewer_id < len(self.players) else None

        # 奥秘堆视图
        mystery_view = []
        for i in range(MYSTERY_DECK_SIZE):
            entry = {"idx": i, "revealed": self.mystery_revealed[i]}
            if reveal_all:
                entry["card"] = self.mystery_deck[i]
            elif viewer and i in viewer.peeked_mysteries:
                entry["card"] = viewer.peeked_mysteries[i]
            entry["seen_by_me"] = bool(viewer and i in viewer.peeked_mysteries) or spectator
            mystery_view.append(entry)

        # 玩家视图
        players_data = []
        for p in self.players:
            if spectator:
                # 观战者看到所有手牌
                d = p.to_public()
                d["peeked_mysteries"] = p.peeked_mysteries
                players_data.append(d)
            elif p.id == viewer_id:
                if reveal_all or p.eliminated:
                    # 游戏结束、或者 viewer 已死亡 → 看见自己的牌
                    players_data.append(p.to_dead_self())
                else:
                    players_data.append(p.to_self())
            else:
                players_data.append(p.to_public())

        return {
            "started": self.started,
            "player_count": self.player_count,
            "current_player": self.current_player,
            "min_callable": self.min_callable,
            "has_called_this_turn": self.has_called_this_turn,
            "discard_counts": self.discard_counts,
            "deck_size": len(self.deck),
            "mystery": mystery_view,
            "game_over": self.game_over,
            "winner_id": self.winner_id,
            "log": self.log[-60:],
            "viewer_id": viewer_id if not spectator else -1,
            "is_spectator": spectator,
            "turn_deadline": self.turn_deadline,
            "pending": (
                self.pending if (self.pending and not spectator and self.pending.get("player_id") == viewer_id)
                else (
                    {"kind": self.pending["kind"], "player_id": self.pending["player_id"]}
                    if self.pending else None
                )
            ),
            "players": players_data,
        }
