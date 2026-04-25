"""
AstrBot Galgame 辅助插件
版本: 2.1.0
新增: WebUI 可视化配置支持（_conf_schema.json）
"""

import json
import os
import re
import random
import aiohttp
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig


@register(
    name="astrbot_plugin_galgame",
    desc="Galgame 辅助插件 v2.1 - 攻略/记录/CG收集/评分/VNDB，支持 WebUI 配置",
    version="2.1.0",
    author="GalHelper",
)
class GalgamePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # ── WebUI 配置（从 _conf_schema.json 注入） ──
        self.cfg = config  # 完整配置对象，支持 .save_config()

        # AI 设置
        ai = config.get("ai_settings", {})
        self.ai_provider_id: str   = ai.get("ai_provider", "")
        self.ai_system_prompt: str = ai.get(
            "ai_system_prompt",
            "你是一位资深的 Galgame 爱好者和攻略专家，对各大名作如数家珍，"
            "擅长提供无剧透的游玩建议。请用中文回答，语气友好亲切。"
        )
        self.enable_spoiler_hint: bool = ai.get("enable_spoiler_hint", True)

        # VNDB 设置
        vndb = config.get("vndb_settings", {})
        self.vndb_timeout: int        = vndb.get("vndb_timeout", 10)
        self.vndb_top_min_votes: int  = vndb.get("vndb_top_min_votes", 1000)
        self.vndb_top_count: int      = vndb.get("vndb_top_count", 10)

        # 评分设置
        rating = config.get("rating_settings", {})
        self.rating_max_score: float  = rating.get("rating_max_score", 10.0)
        self.ranking_show_count: int  = rating.get("ranking_show_count", 15)

        # CG 设置
        cg = config.get("cg_settings", {})
        self.cg_bar_length: int        = cg.get("cg_bar_length", 20)
        self.cg_completion_msg: str    = cg.get("cg_completion_message", "🎊 CG 全收集！完美通关！")

        # 笔记设置
        note = config.get("note_settings", {})
        self.max_notes_per_game: int  = note.get("max_notes_per_game", 50)

        # 推荐设置
        rec = config.get("recommend_settings", {})
        self.top_show_count: int      = min(rec.get("top_show_count", 12), 12)
        self.tag_recommend_count: int = rec.get("tag_recommend_count", 5)

        # ── 数据存储 ──
        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.progress_file = os.path.join(self.data_dir, "progress.json")
        self.wishlist_file = os.path.join(self.data_dir, "wishlist.json")
        self.notes_file    = os.path.join(self.data_dir, "notes.json")
        self.cg_file       = os.path.join(self.data_dir, "cg.json")
        self.rating_file   = os.path.join(self.data_dir, "ratings.json")

        self.progress_data = self._load_json(self.progress_file)
        self.wishlist_data = self._load_json(self.wishlist_file)
        self.notes_data    = self._load_json(self.notes_file)
        self.cg_data       = self._load_json(self.cg_file)
        self.rating_data   = self._load_json(self.rating_file)

        self.VNDB_API = "https://api.vndb.org/kana"
        logger.info(
            f"[GalgamePlugin v2.1] 已加载 ✅\n"
            f"  AI提供商={'默认' if not self.ai_provider_id else self.ai_provider_id}\n"
            f"  VNDB超时={self.vndb_timeout}s  榜单数={self.vndb_top_count}\n"
            f"  评分满分={self.rating_max_score}  CG进度条长={self.cg_bar_length}"
        )

    # ─────────────── 工具方法 ───────────────

    def _load_json(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_json(self, path: str, data: dict):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _uid(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _stars(self, score: float) -> str:
        """将分数转换为星星，适配自定义满分"""
        ratio  = score / self.rating_max_score  # 0.0~1.0
        filled = int(ratio * 5)
        half   = 1 if (ratio * 5 - filled) >= 0.5 else 0
        empty  = 5 - filled - half
        return "★" * filled + "½" * half + "☆" * empty + f"  {score:.1f}/{self.rating_max_score:.0f}"

    def _cg_bar(self, collected: int, total: int) -> str:
        """生成可配置长度的进度条"""
        pct    = collected / total if total > 0 else 0
        length = self.cg_bar_length
        done   = int(pct * length)
        return f"[{'█' * done}{'░' * (length - done)}] {pct * 100:.1f}%"

    async def _ai_chat(self, prompt: str, event: AstrMessageEvent):
        """统一 AI 调用入口，支持指定提供商，含错误处理"""
        full_prompt = f"{self.ai_system_prompt}\n\n{prompt}" if self.ai_system_prompt else prompt
        try:
            if self.ai_provider_id:
                provider = self.context.get_provider_by_id(self.ai_provider_id)
            else:
                provider = self.context.get_using_provider()
            if provider is None:
                yield event.plain_result("❌ 未找到可用的 AI 提供商，请在 AstrBot WebUI → 服务提供商 中配置并启用模型。")
                return
            result = await provider.text_chat(prompt=full_prompt, session_id=event.session_id)
            yield result
        except Exception as e:
            err = str(e)
            if "401" in err or "token" in err.lower() or "authentication" in err.lower():
                yield event.plain_result(
                    "❌ AI 提供商认证失败（401）\n"
                    "请前往 AstrBot WebUI → 服务提供商，\n"
                    "重新填写 API Key 或重新登录账号后再试。"
                )
            else:
                yield event.plain_result(f"❌ AI 调用出错：{err}")
            logger.error(f"[GalgamePlugin] AI调用异常: {e}")

    # ═══════════════════════════════════════
    #  1. 帮助菜单
    # ═══════════════════════════════════════

    @filter.command("gal help")
    async def gal_help(self, event: AstrMessageEvent):
        """显示 Galgame 插件的所有可用指令和当前配置信息"""
        text = (
            "🎮 ══ Galgame 辅助插件 v2.1 ══ 🎮\n\n"
            "📖 【攻略查询 (AI)】\n"
            "  /gal se <游戏名>               综合攻略建议\n"
            "  /gal route  <游戏名>           推荐游玩路线\n"
            "  /gal endings <游戏名>          结局列表\n"
            "  /gal char <游戏名> <角色>      角色攻略\n\n"
            "🗄️ 【VNDB 数据库】\n"
            "  /gal vn <游戏名>               按名称查询\n"
            "  /gal vnid <v12345>             按 ID 精确查询\n"
            "  /gal vntop                     VNDB 高分榜\n\n"
            "📝 【进度记录】\n"
            "  /gal add / done / pause <游戏名>\n"
            "  /gal progress\n"
            "  /gal note <游戏名> <内容>  /  /gal notes <游戏名>\n\n"
            "🖼️ 【CG 收集】\n"
            "  /gal cg init / add / set / show / list\n\n"
            "⭐ 【评分系统】\n"
            "  /gal rate <游戏名> <分数> [评语]\n"
            "  /gal myratings / ranking / review <游戏名>\n\n"
            "⭐ 【心愿单】\n"
            "  /gal wish / wishlist / unwish\n\n"
            "🎲 【推荐】\n"
            "  /gal recommend / top / tag <标签>\n\n"
            "💬 【AI 讨论】\n"
            "  /gal talk <内容>\n\n"
            "⚙️ 【当前配置】\n"
            f"  AI 提供商：{'默认' if not self.ai_provider_id else self.ai_provider_id}\n"
            f"  评分满分：{self.rating_max_score:.0f}  VNDB超时：{self.vndb_timeout}s\n"
            f"  （WebUI → 插件 → Galgame插件 → 配置 可修改）\n\n"
            "  /gal help  显示此菜单"
        )
        yield event.plain_result(text)

    # ═══════════════════════════════════════
    #  2. 攻略查询 (AI)
    # ═══════════════════════════════════════

    @filter.command("gal se")
    async def gal_search(self, event: AstrMessageEvent):
        """【攻略查询】AI 综合攻略：游戏简介、游玩建议、路线顺序、新手注意事项。用法：/gal se <游戏名>"""
        args = event.message_str.strip().removeprefix("/gal se").strip()
        if not args:
            yield event.plain_result("❌ 格式：/gal se <游戏名>")
            return
        spoiler_note = "如涉及剧情请加剧透警告。" if self.enable_spoiler_hint else ""
        prompt = (
            f"请介绍《{args}》的攻略信息：\n"
            f"1.简介(2-3句,无剧透) 2.游玩建议 3.推荐路线顺序 4.新手注意事项\n{spoiler_note}"
        )
        async for r in self._ai_chat(prompt, event):
            yield r

    @filter.command("gal route")
    async def gal_route(self, event: AstrMessageEvent):
        """【攻略查询】AI 推荐游玩路线顺序，说明每条线路的先后原因。用法：/gal route <游戏名>"""
        args = event.message_str.strip().removeprefix("/gal route").strip()
        if not args:
            yield event.plain_result("❌ 格式：/gal route <游戏名>")
            return
        spoiler_note = "剧透内容加||警告||标注。" if self.enable_spoiler_hint else ""
        prompt = f"为《{args}》提供推荐游玩路线顺序。格式：线路名 → 原因。{spoiler_note}"
        async for r in self._ai_chat(prompt, event):
            yield r

    @filter.command("gal endings")
    async def gal_endings(self, event: AstrMessageEvent):
        """【攻略查询】列出游戏所有结局名称（不剧透）及类型（Good/Normal/Bad End）。用法：/gal endings <游戏名>"""
        args = event.message_str.strip().removeprefix("/gal endings").strip()
        if not args:
            yield event.plain_result("❌ 格式：/gal endings <游戏名>")
            return
        prompt = f"列出《{args}》结局列表（只列名称不剧透）。格式：序号. 结局名 [Good/Normal/Bad End]"
        async for r in self._ai_chat(prompt, event):
            yield r

    @filter.command("gal char")
    async def gal_char(self, event: AstrMessageEvent):
        """【攻略查询】AI 查询特定角色攻略要点：角色简介、好感度注意、进入路线的关键选择。用法：/gal char <游戏名> <角色名>"""
        args = event.message_str.strip().removeprefix("/gal char").strip().split(None, 1)
        if len(args) < 2:
            yield event.plain_result("❌ 格式：/gal char <游戏名> <角色名>")
            return
        game, char = args[0], args[1]
        spoiler_note = "重大剧情加警告。" if self.enable_spoiler_hint else ""
        prompt = (
            f"介绍《{game}》中【{char}】的攻略要点：\n"
            f"1.角色简介(无剧透) 2.好感度/选项注意 3.进入该线路的关键选择。{spoiler_note}"
        )
        async for r in self._ai_chat(prompt, event):
            yield r

    # ═══════════════════════════════════════
    #  3. VNDB 数据库
    # ═══════════════════════════════════════

    async def _vndb_post(self, endpoint: str, payload: dict) -> dict | None:
        url = f"{self.VNDB_API}/{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=self.vndb_timeout)  # ← 使用配置超时
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"[VNDB] HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"[VNDB] 请求失败: {e}")
            return None

    def _fmt_vndb(self, vn: dict) -> str:
        title     = vn.get("title", "未知")
        alttitle  = vn.get("alttitle") or ""
        released  = vn.get("released") or "未知"
        rating    = vn.get("rating")
        votecount = vn.get("votecount", 0)
        length    = vn.get("length_minutes")
        devnames  = "、".join(d.get("name","") for d in vn.get("developers",[])) or "未知"
        top_tags  = [t["name"] for t in vn.get("tags",[])[:6] if t.get("spoiler",0)==0]
        desc_raw  = vn.get("description") or ""
        desc      = re.sub(r'\[/?[a-zA-Z][^\]]*\]', '', desc_raw)[:220].strip()
        score_str = f"{rating:.2f}/100 （{votecount} 票）" if rating else "暂无评分"
        len_str   = f"约 {length // 60} 小时" if length else "未知"
        lines = [f"📚 ══ VNDB 数据 ══", f"🎮 {title}"]
        if alttitle:
            lines.append(f"   ({alttitle})")
        lines += [
            "", f"🏢 开发商：{devnames}",
            f"📅 发售日：{released}", f"⏱️  游玩时长：{len_str}",
            f"⭐ VNDB 评分：{score_str}",
        ]
        if top_tags:
            lines.append(f"🏷️  标签：{'  '.join(top_tags)}")
        if desc:
            lines.append(f"\n📖 简介：{desc}{'…' if len(desc_raw) > 220 else ''}")
        return "\n".join(lines)

    @filter.command("gal vn")
    async def gal_vn(self, event: AstrMessageEvent):
        """【VNDB】按游戏名查询 VNDB 数据库，返回评分、发售日、时长、标签、简介。用法：/gal vn <游戏名>"""
        name = event.message_str.strip().removeprefix("/gal vn").strip()
        if not name:
            yield event.plain_result("❌ 格式：/gal vn <游戏名>")
            return
        yield event.plain_result(f"🔍 正在查询 VNDB：{name} ...")
        payload = {
            "filters": ["search", "=", name],
            "fields": "title,alttitle,released,rating,votecount,length_minutes,developers.name,tags.name,tags.spoiler,description",
            "sort": "searchrank", "results": 1,
        }
        data = await self._vndb_post("vn", payload)
        if not data or not data.get("results"):
            yield event.plain_result(f"❌ VNDB 未找到《{name}》，建议尝试英文或日文原名")
            return
        vn = data["results"][0]
        yield event.plain_result(self._fmt_vndb(vn) + f"\n\n🔗 https://vndb.org/{vn.get('id','')}")

    @filter.command("gal vnid")
    async def gal_vnid(self, event: AstrMessageEvent):
        """【VNDB】按 VNDB ID 精确查询游戏条目。用法：/gal vnid <v12345>，例如 /gal vnid v4"""
        vid = event.message_str.strip().removeprefix("/gal vnid").strip()
        if not vid or not vid.startswith("v"):
            yield event.plain_result("❌ 格式：/gal vnid <v12345>")
            return
        yield event.plain_result(f"🔍 查询 VNDB {vid} ...")
        payload = {
            "filters": ["id", "=", vid],
            "fields": "title,alttitle,released,rating,votecount,length_minutes,developers.name,tags.name,tags.spoiler,description",
            "results": 1,
        }
        data = await self._vndb_post("vn", payload)
        if not data or not data.get("results"):
            yield event.plain_result(f"❌ 未找到 ID {vid}")
            return
        vn = data["results"][0]
        yield event.plain_result(self._fmt_vndb(vn) + f"\n\n🔗 https://vndb.org/{vid}")

    @filter.command("gal vntop")
    async def gal_vntop(self, event: AstrMessageEvent):
        """【VNDB】实时拉取 VNDB 评分排行榜，显示高分游戏列表（数量和票数门槛可在 WebUI 配置）"""
        yield event.plain_result(
            f"🔍 正在获取 VNDB 高分榜（最低 {self.vndb_top_min_votes} 票）..."
        )
        payload = {
            "filters": ["votecount", ">=", self.vndb_top_min_votes],  # ← 使用配置票数
            "fields": "title,rating,votecount,released",
            "sort": "rating", "reverse": True,
            "results": self.vndb_top_count,  # ← 使用配置数量
        }
        data = await self._vndb_post("vn", payload)
        if not data or not data.get("results"):
            yield event.plain_result("❌ 获取榜单失败，请稍后再试")
            return
        lines = [f"🏆 ══ VNDB 评分 Top{self.vndb_top_count} ══\n"]
        for i, vn in enumerate(data["results"], 1):
            year = (vn.get("released") or "")[:4]
            lines.append(
                f"  {i:2d}. 《{vn.get('title','未知')}》({year})\n"
                f"      ⭐ {vn.get('rating',0):.2f}/100  ({vn.get('votecount',0)} 票)"
            )
        lines.append("\n数据来源：vndb.org")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  4. 进度记录
    # ═══════════════════════════════════════

    @filter.command("gal add")
    async def gal_add(self, event: AstrMessageEvent):
        """【进度记录】将游戏添加到个人在玩列表，记录开始日期。用法：/gal add <游戏名>"""
        uid  = self._uid(event)
        game = event.message_str.strip().removeprefix("/gal add").strip()
        if not game:
            yield event.plain_result("❌ 请提供游戏名称")
            return
        self.progress_data.setdefault(uid, {})
        if game in self.progress_data[uid]:
            yield event.plain_result(f"⚠️ 《{game}》已在列表（{self.progress_data[uid][game]['status']}）")
            return
        self.progress_data[uid][game] = {"status": "在玩", "added_at": datetime.now().strftime("%Y-%m-%d")}
        self._save_json(self.progress_file, self.progress_data)
        yield event.plain_result(f"✅ 《{game}》已加入在玩列表！祝游玩愉快 🎮")

    @filter.command("gal done")
    async def gal_done(self, event: AstrMessageEvent):
        """【进度记录】将游戏标记为已通关，记录通关日期并提示打分。用法：/gal done <游戏名>"""
        uid  = self._uid(event)
        game = event.message_str.strip().removeprefix("/gal done").strip()
        if not game:
            yield event.plain_result("❌ 请提供游戏名称")
            return
        self.progress_data.setdefault(uid, {}).setdefault(game, {"added_at": datetime.now().strftime("%Y-%m-%d")})
        self.progress_data[uid][game].update({"status": "已通关", "done_at": datetime.now().strftime("%Y-%m-%d")})
        self._save_json(self.progress_file, self.progress_data)
        yield event.plain_result(
            f"🎉 恭喜通关《{game}》！  📅 {datetime.now().strftime('%Y-%m-%d')}\n\n"
            f"记得留下评分：/gal rate {game} <分数>"
        )

    @filter.command("gal pause")
    async def gal_pause(self, event: AstrMessageEvent):
        """【进度记录】将游戏标记为搁置状态（坑游戏专用）。用法：/gal pause <游戏名>"""
        uid  = self._uid(event)
        game = event.message_str.strip().removeprefix("/gal pause").strip()
        if not game:
            yield event.plain_result("❌ 请提供游戏名称")
            return
        self.progress_data.setdefault(uid, {}).setdefault(game, {"added_at": datetime.now().strftime("%Y-%m-%d")})
        self.progress_data[uid][game]["status"] = "搁置"
        self._save_json(self.progress_file, self.progress_data)
        yield event.plain_result(f"⏸ 《{game}》已标记为搁置，有缘再见！")

    @filter.command("gal progress")
    async def gal_progress(self, event: AstrMessageEvent):
        """【进度记录】查看个人游玩记录总览，按在玩/已通关/搁置分组展示所有游戏"""
        uid   = self._uid(event)
        games = self.progress_data.get(uid, {})
        if not games:
            yield event.plain_result("📭 还没有记录，/gal add <游戏名> 开始吧！")
            return
        lines = ["📊 ══ 我的游玩记录 ══\n"]
        for status, icon in [("在玩","🎮"), ("已通关","✅"), ("搁置","⏸")]:
            grp = [(g, v) for g, v in games.items() if v.get("status") == status]
            if grp:
                lines.append(f"{icon} {status} ({len(grp)})：")
                for g, v in grp:
                    dk = "done_at" if status == "已通关" else "added_at"
                    lines.append(f"  • {g}  ({v.get(dk,'未知')})")
                lines.append("")
        lines.append(f"共记录 {len(games)} 款")
        yield event.plain_result("\n".join(lines))

    @filter.command("gal note")
    async def gal_note(self, event: AstrMessageEvent):
        """【进度记录】为指定游戏添加个人笔记，支持记录攻略提示、剧情感想等。用法：/gal note <游戏名> <内容>"""
        uid  = self._uid(event)
        args = event.message_str.strip().removeprefix("/gal note").strip().split(None, 1)
        if len(args) < 2:
            yield event.plain_result("❌ 格式：/gal note <游戏名> <内容>")
            return
        game, content = args[0], args[1]
        notes = self.notes_data.setdefault(uid, {}).setdefault(game, [])
        # 检查笔记数量上限
        if self.max_notes_per_game > 0 and len(notes) >= self.max_notes_per_game:
            yield event.plain_result(
                f"⚠️ 《{game}》笔记已达上限 {self.max_notes_per_game} 条\n"
                f"（管理员可在 WebUI 修改「每款游戏最大笔记数量」）"
            )
            return
        notes.append({"content": content, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
        self._save_json(self.notes_file, self.notes_data)
        yield event.plain_result(f"📝 笔记已保存！（{len(notes)}/{self.max_notes_per_game if self.max_notes_per_game > 0 else '∞'}）\n《{game}》：{content}")

    @filter.command("gal notes")
    async def gal_notes(self, event: AstrMessageEvent):
        """【进度记录】查看某款游戏保存的所有个人笔记，按时间顺序列出。用法：/gal notes <游戏名>"""
        uid  = self._uid(event)
        game = event.message_str.strip().removeprefix("/gal notes").strip()
        if not game:
            yield event.plain_result("❌ 格式：/gal notes <游戏名>")
            return
        notes = self.notes_data.get(uid, {}).get(game, [])
        if not notes:
            yield event.plain_result(f"📭 《{game}》暂无笔记")
            return
        lines = [f"📒 《{game}》笔记 ({len(notes)} 条)\n"]
        for i, n in enumerate(notes, 1):
            lines.append(f"{i}. [{n['time']}]\n   {n['content']}")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  5. CG 收集记录
    # ═══════════════════════════════════════

    @filter.command("gal cg init")
    async def gal_cg_init(self, event: AstrMessageEvent):
        """【CG收集】初始化某游戏的 CG 收集记录，设定总张数。用法：/gal cg init <游戏名> <CG总数>"""
        uid  = self._uid(event)
        args = event.message_str.strip().removeprefix("/gal cg init").strip().split(None, 1)
        if len(args) < 2:
            yield event.plain_result("❌ 格式：/gal cg init <游戏名> <CG总数>")
            return
        game = args[0]
        try:
            total = int(args[1]); assert total > 0
        except Exception:
            yield event.plain_result("❌ CG总数必须是正整数")
            return
        self.cg_data.setdefault(uid, {})
        if game in self.cg_data[uid]:
            yield event.plain_result(
                f"⚠️ 《{game}》CG记录已存在（总计 {self.cg_data[uid][game]['total']} 张）\n"
                f"使用 /gal cg set {game} <数量> 更新"
            )
            return
        self.cg_data[uid][game] = {
            "total": total, "collected": 0,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
        }
        self._save_json(self.cg_file, self.cg_data)
        yield event.plain_result(
            f"🖼️ 《{game}》CG收集已初始化！\n"
            f"总计：{total} 张  已收集：0 张\n"
            f"更新进度：/gal cg add {game} <数量>"
        )

    @filter.command("gal cg add")
    async def gal_cg_add(self, event: AstrMessageEvent):
        """【CG收集】在现有数量基础上累加已收集 CG 数，并显示进度条。用法：/gal cg add <游戏名> <新增数量>"""
        uid  = self._uid(event)
        args = event.message_str.strip().removeprefix("/gal cg add").strip().split(None, 1)
        if len(args) < 2:
            yield event.plain_result("❌ 格式：/gal cg add <游戏名> <数量>")
            return
        game = args[0]
        try:
            n = int(args[1])
        except ValueError:
            yield event.plain_result("❌ 数量必须是整数")
            return
        rec = self.cg_data.get(uid, {}).get(game)
        if not rec:
            yield event.plain_result(f"❌ 《{game}》未初始化，先用 /gal cg init {game} <总数>")
            return
        old = rec["collected"]
        rec["collected"]  = min(old + n, rec["total"])
        rec["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        self._save_json(self.cg_file, self.cg_data)
        bar = self._cg_bar(rec["collected"], rec["total"])
        # 使用配置的庆祝消息
        tip = f"\n{self.cg_completion_msg}" if rec["collected"] == rec["total"] else ""
        yield event.plain_result(
            f"🖼️ 《{game}》CG 更新\n"
            f"  {old} → {rec['collected']} / {rec['total']} 张\n"
            f"  {bar}{tip}"
        )

    @filter.command("gal cg set")
    async def gal_cg_set(self, event: AstrMessageEvent):
        """【CG收集】直接设置已收集 CG 的绝对数量（覆盖原有数量）。用法：/gal cg set <游戏名> <数量>"""
        uid  = self._uid(event)
        args = event.message_str.strip().removeprefix("/gal cg set").strip().split(None, 1)
        if len(args) < 2:
            yield event.plain_result("❌ 格式：/gal cg set <游戏名> <数量>")
            return
        game = args[0]
        try:
            n = int(args[1])
        except ValueError:
            yield event.plain_result("❌ 数量必须是整数")
            return
        rec = self.cg_data.get(uid, {}).get(game)
        if not rec:
            yield event.plain_result(f"❌ 《{game}》未初始化，先用 /gal cg init {game} <总数>")
            return
        rec["collected"]  = max(0, min(n, rec["total"]))
        rec["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        self._save_json(self.cg_file, self.cg_data)
        bar = self._cg_bar(rec["collected"], rec["total"])
        yield event.plain_result(
            f"🖼️ 《{game}》CG 更新\n"
            f"  已收集：{rec['collected']} / {rec['total']} 张\n"
            f"  {bar}"
        )

    @filter.command("gal cg show")
    async def gal_cg_show(self, event: AstrMessageEvent):
        """【CG收集】查看某游戏的 CG 收集进度条、百分比和剩余数量。用法：/gal cg show <游戏名>"""
        uid  = self._uid(event)
        game = event.message_str.strip().removeprefix("/gal cg show").strip()
        if not game:
            yield event.plain_result("❌ 格式：/gal cg show <游戏名>")
            return
        rec = self.cg_data.get(uid, {}).get(game)
        if not rec:
            yield event.plain_result(f"❌ 《{game}》尚未初始化 CG 记录")
            return
        bar    = self._cg_bar(rec["collected"], rec["total"])
        pct    = rec["collected"] / rec["total"] * 100 if rec["total"] else 0
        remain = rec["total"] - rec["collected"]
        status = self.cg_completion_msg if remain == 0 else f"还差 {remain} 张"
        yield event.plain_result(
            f"🖼️ ══ 《{game}》CG 进度 ══\n\n"
            f"  已收集：{rec['collected']} / {rec['total']} 张\n"
            f"  {bar}\n"
            f"  {pct:.1f}%  {status}\n"
            f"  更新于：{rec.get('updated_at', rec.get('created_at','未知'))}"
        )

    @filter.command("gal cg list")
    async def gal_cg_list(self, event: AstrMessageEvent):
        """【CG收集】一览所有游戏的 CG 收集概况，带简短进度条和百分比"""
        uid    = self._uid(event)
        all_cg = self.cg_data.get(uid, {})
        if not all_cg:
            yield event.plain_result("📭 还没有 CG 记录，/gal cg init <游戏名> <总数> 开始！")
            return
        lines = [f"🖼️ ══ 我的 CG 收集 ({len(all_cg)} 款) ══\n"]
        for game, rec in all_cg.items():
            c, t  = rec["collected"], rec["total"]
            pct   = c / t * 100 if t else 0
            done  = int(pct / 10)
            bar   = "█" * done + "░" * (10 - done)
            mark  = "🎊" if c == t else f"{pct:.0f}%"
            lines.append(f"  {mark}  《{game}》  {c}/{t}  [{bar}]")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  6. 评分系统
    # ═══════════════════════════════════════

    @filter.command("gal rate")
    async def gal_rate(self, event: AstrMessageEvent):
        uid  = self._uid(event)
        args = event.message_str.strip().removeprefix("/gal rate").strip().split(None, 2)
        if len(args) < 2:
            yield event.plain_result(
                f"❌ 格式：/gal rate <游戏名> <1-{self.rating_max_score:.0f}> [评语]\n"
                f"例如：/gal rate Clannad 10 神作，催泪无敌"
            )
            return
        game = args[0]
        try:
            score = float(args[1])
            assert 1 <= score <= self.rating_max_score  # ← 使用配置的满分值
        except Exception:
            yield event.plain_result(f"❌ 分数必须在 1-{self.rating_max_score:.0f} 之间")
            return
        comment = args[2].strip() if len(args) > 2 else ""
        self.rating_data.setdefault(game, {})[uid] = {
            "score": score, "comment": comment,
            "time": datetime.now().strftime("%Y-%m-%d"),
        }
        self._save_json(self.rating_file, self.rating_data)
        all_scores = [v["score"] for v in self.rating_data[game].values()]
        avg = sum(all_scores) / len(all_scores)
        yield event.plain_result(
            f"⭐ 评分已记录！\n\n"
            f"🎮 《{game}》\n"
            f"  你的评分：{self._stars(score)}\n"
            + (f"  💬 {comment}\n" if comment else "") +
            f"\n  群内均分：{self._stars(avg)}（共 {len(all_scores)} 人）"
        )

    @filter.command("gal myratings")
    async def gal_myratings(self, event: AstrMessageEvent):
        uid = self._uid(event)
        my  = [(g, v[uid]) for g, v in self.rating_data.items() if uid in v]
        if not my:
            yield event.plain_result("📭 你还没有评过分，/gal rate <游戏名> <分数> 开始！")
            return
        my.sort(key=lambda x: x[1]["score"], reverse=True)
        lines = [f"⭐ ══ 我的评分记录 ({len(my)} 款) ══\n"]
        for game, rec in my:
            lines.append(f"《{game}》")
            lines.append(f"  {self._stars(rec['score'])}")
            if rec.get("comment"):
                lines.append(f"  💬 {rec['comment']}")
            lines.append(f"  📅 {rec['time']}\n")
        yield event.plain_result("\n".join(lines))

    @filter.command("gal ranking")
    async def gal_ranking(self, event: AstrMessageEvent):
        if not self.rating_data:
            yield event.plain_result("📭 还没有任何评分！")
            return
        ranked = []
        for game, votes in self.rating_data.items():
            if not votes: continue
            scores = [v["score"] for v in votes.values()]
            ranked.append((game, sum(scores)/len(scores), len(scores)))
        ranked.sort(key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines  = ["🏆 ══ 群内 Galgame 评分榜 ══\n"]
        for i, (game, avg, cnt) in enumerate(ranked[:self.ranking_show_count]):  # ← 使用配置数量
            medal = medals[i] if i < 3 else f"  {i+1}."
            lines.append(f"{medal} 《{game}》")
            lines.append(f"     {self._stars(avg)}  （{cnt} 人）\n")
        yield event.plain_result("\n".join(lines))

    @filter.command("gal review")
    async def gal_review(self, event: AstrMessageEvent):
        game  = event.message_str.strip().removeprefix("/gal review").strip()
        if not game:
            yield event.plain_result("❌ 格式：/gal review <游戏名>")
            return
        votes = self.rating_data.get(game)
        if not votes:
            yield event.plain_result(f"📭 《{game}》暂无评价，/gal rate {game} <分数> 第一个打分！")
            return
        scores = [v["score"] for v in votes.values()]
        avg    = sum(scores) / len(scores)
        dist   = {}
        for s in scores:
            k = str(int(s)); dist[k] = dist.get(k, 0) + 1
        lines = [
            f"📊 ══ 《{game}》评价汇总 ══\n",
            f"  均分：{self._stars(avg)}",
            f"  评分人数：{len(scores)} 人\n  分布：",
        ]
        for sv in range(int(self.rating_max_score), 0, -1):
            cnt = dist.get(str(sv), 0)
            lines.append(f"  {sv:2d}分 {'█' * cnt} ({cnt})")
        lines.append("\n─── 玩家评语 ───")
        has_comment = False
        for rec in votes.values():
            if rec.get("comment"):
                lines.append(f"  💬 {rec['score']}分：{rec['comment']}  ({rec['time']})")
                has_comment = True
        if not has_comment:
            lines.append("  （暂无评语）")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  7. 心愿单
    # ═══════════════════════════════════════

    @filter.command("gal wish")
    async def gal_wish(self, event: AstrMessageEvent):
        uid  = self._uid(event)
        game = event.message_str.strip().removeprefix("/gal wish").strip()
        if not game:
            yield event.plain_result("❌ 请提供游戏名称")
            return
        self.wishlist_data.setdefault(uid, [])
        if game in self.wishlist_data[uid]:
            yield event.plain_result(f"⚠️ 《{game}》已在心愿单！")
            return
        self.wishlist_data[uid].append(game)
        self._save_json(self.wishlist_file, self.wishlist_data)
        yield event.plain_result(f"⭐ 《{game}》加入心愿单！(ﾉ◕ヮ◕)ﾉ")

    @filter.command("gal wishlist")
    async def gal_wishlist(self, event: AstrMessageEvent):
        uid      = self._uid(event)
        wishlist = self.wishlist_data.get(uid, [])
        if not wishlist:
            yield event.plain_result("📭 心愿单是空的，/gal wish <游戏名> 来添加！")
            return
        lines = [f"⭐ 我的心愿单 ({len(wishlist)} 款)\n"]
        for i, g in enumerate(wishlist, 1):
            lines.append(f"  {i}. {g}")
        yield event.plain_result("\n".join(lines))

    @filter.command("gal unwish")
    async def gal_unwish(self, event: AstrMessageEvent):
        uid      = self._uid(event)
        game     = event.message_str.strip().removeprefix("/gal unwish").strip()
        wishlist = self.wishlist_data.get(uid, [])
        if game in wishlist:
            wishlist.remove(game)
            self._save_json(self.wishlist_file, self.wishlist_data)
            yield event.plain_result(f"🗑️ 已从心愿单移除《{game}》")
        else:
            yield event.plain_result(f"❌ 心愿单中没有《{game}》")

    # ═══════════════════════════════════════
    #  8. 推荐系统
    # ═══════════════════════════════════════

    CLASSIC_GAMES = [
        ("Clannad", "Key社经典催泪神作"),
        ("Little Busters!", "Key社青春热血之作"),
        ("白色相簿2", "现实系恋爱三角剧情"),
        ("月姬", "TYPE-MOON原点作品"),
        ("Fate/stay night", "TYPE-MOON三路线神作"),
        ("Steins;Gate", "科学ADV时间旅行名作"),
        ("ef - a fairy tale of the two.", "画面音乐双绝爱情故事"),
        ("Muv-Luv Alternative", "史诗级转折的巅峰之作"),
        ("ISLAND", "孤岛跨时空恋爱"),
        ("Summer Pockets", "Key社近年夏日力作"),
        ("AIR", "Key社三大催泪作之一"),
        ("planetarian", "废墟中与机器人的邂逅"),
    ]

    @filter.command("gal recommend")
    async def gal_recommend(self, event: AstrMessageEvent):
        game, desc = random.choice(self.CLASSIC_GAMES)
        yield event.plain_result(
            f"🎲 随机推荐：\n\n🎮 《{game}》\n📖 {desc}\n\n"
            f"  攻略：/gal se {game}\n"
            f"  评价：/gal review {game}\n"
            f"  VNDB：/gal vn {game}"
        )

    @filter.command("gal top")
    async def gal_top(self, event: AstrMessageEvent):
        count = self.top_show_count  # ← 使用配置数量
        lines = [f"🏆 ══ 经典 Galgame 推荐榜 (Top{count}) ══\n"]
        for i, (game, desc) in enumerate(self.CLASSIC_GAMES[:count], 1):
            lines.append(f"  {i:2d}. 《{game}》\n      {desc}")
        lines.append("\n  /gal vntop 查看 VNDB 实时评分榜")
        yield event.plain_result("\n".join(lines))

    @filter.command("gal tag")
    async def gal_tag(self, event: AstrMessageEvent):
        tag = event.message_str.strip().removeprefix("/gal tag").strip()
        if not tag:
            yield event.plain_result("❌ 格式：/gal tag <标签>  如：/gal tag 催泪")
            return
        prompt = (
            f"推荐 {self.tag_recommend_count} 款标签为【{tag}】的优秀 Galgame。\n"  # ← 使用配置数量
            "格式：序号. 《游戏名》- 一句话描述。只推荐真实存在的游戏。"
        )
        async for r in self._ai_chat(prompt, event):
            yield r

    # ═══════════════════════════════════════
    #  9. AI 剧情讨论
    # ═══════════════════════════════════════

    @filter.command("gal talk")
    async def gal_talk(self, event: AstrMessageEvent):
        content = event.message_str.strip().removeprefix("/gal talk").strip()
        if not content:
            yield event.plain_result(
                "💬 格式：/gal talk <内容>\n"
                "例如：/gal talk Clannad 结局我哭惨了"
            )
            return
        prompt = f"以同好身份友好讨论以下 Galgame 话题，语气自然：\n\n用户说：{content}"
        async for r in self._ai_chat(prompt, event):
            yield r

    # ═══════════════════════════════════════
    #  10. 提醒
    # ═══════════════════════════════════════

    @filter.command("gal remind")
    async def gal_remind(self, event: AstrMessageEvent):
        args = event.message_str.strip().removeprefix("/gal remind").strip()
        if not args:
            yield event.plain_result("❌ 格式：/gal remind <游戏名> <时间描述>")
            return
        yield event.plain_result(f"⏰ 游玩提醒已记录：{args}\n请记得按时游玩！")
