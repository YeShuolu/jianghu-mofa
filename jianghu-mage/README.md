# 江湖魔法师 · 联机版

单房间 · 3-6 人 · 匿名进入 · 实时同步

## 📁 项目结构

```
jianghu-mage/
├── server/
│   ├── main.py           # FastAPI + WebSocket 服务器
│   ├── game.py           # 游戏核心逻辑（权威判定）
│   └── requirements.txt
├── static/
│   └── index.html        # 前端（大厅 + 游戏）
├── render.yaml           # Render 部署配置
└── README.md
```

---

## 🚀 部署到 Render（推荐 · 免费）

### 步骤 1：把代码推到 GitHub

1. 在 GitHub 新建一个仓库（比如 `jianghu-mage`），可以是 public 也可以是 private
2. 把整个 `jianghu-mage/` 目录推上去：

```bash
cd jianghu-mage
git init
git add .
git commit -m "first version"
git branch -M main
git remote add origin https://github.com/你的用户名/jianghu-mage.git
git push -u origin main
```

### 步骤 2：在 Render 部署

1. 打开 https://render.com 注册（用 GitHub 账号登录最方便）
2. Dashboard → **New +** → **Blueprint**
3. 选择刚才的 GitHub 仓库
4. Render 会自动读取 `render.yaml` 并创建服务
5. 点击 **Apply**，等待构建完成（约 1-2 分钟）

### 步骤 3：拿到网址

部署成功后会给你一个域名，例如：

```
https://jianghu-mage-xxxx.onrender.com
```

把这个网址发给朋友，他们打开就能看到大厅、选座、开始游戏。

> ⚠️ **免费版会休眠**：15 分钟没人访问就会停机，下次访问需要等待 30-60 秒冷启动。开局后保持活跃就不会休眠。

---

## 🧪 本地测试

```bash
cd server
pip install -r requirements.txt
uvicorn main:app --reload
```

访问 http://localhost:8000

多开几个浏览器窗口（或者用隐身模式），分别选不同座位测试 3-6 人局。

---

## 🎮 玩法说明

1. 第一个进入的人会自动占座位 1
2. 复制大厅里的邀请链接发给朋友，他们打开后选空座位即可
3. **3 人**起即可开始；最多 **6 人**
4. 任何已入座玩家都可以点"开始游戏"
5. 游戏结束后点"返回大厅"会重置房间，所有人需要重新加入

---

## ⚙️ 设计说明

- **服务器是权威**：所有规则判定（喊牌对错、扣血、胜利）都在服务器，前端只发指令、收状态
- **看不见自己的牌**：服务器对每个玩家发不同视角的状态（自己只看到 `hand_size`，看不到具体内容）
- **断线重连**：刷新或断网后会自动尝试用原座位号重连，不会丢失游戏状态
- **单房间**：当前实现是全局唯一房间，简单够用。如要多房间，扩展 `Room` 为字典即可

---

## 🐛 已知限制

- 朋友局规模，没做反作弊（理论上可以伪造 WebSocket 消息，但只能控制自己的座位）
- 没有持久化，服务器重启会丢失当前游戏（朋友局重启重玩即可）
- 所有人都能点"开始"和"重置"——朋友间不需要权限管理

---

## 📜 License

仅供学习和朋友间游玩。原版"江湖魔法师"桌游版权归原作者所有。
