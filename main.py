import asyncio
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from PIL import Image as PILImage
from PIL import ImageOps

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


RANKING_URL = "https://www.pixiv.net/novel/ranking.php"
ILLUST_RANKING_URL = "https://www.pixiv.net/ranking.php"
NOVEL_AJAX_URL = "https://www.pixiv.net/ajax/novel/{novel_id}"
NOVEL_PAGE_URL = "https://www.pixiv.net/novel/show.php?id={novel_id}"
ILLUST_AJAX_URL = "https://www.pixiv.net/ajax/illust/{illust_id}"
ILLUST_PAGES_URL = "https://www.pixiv.net/ajax/illust/{illust_id}/pages"
ILLUST_PAGE_URL = "https://www.pixiv.net/artworks/{illust_id}"


@dataclass(slots=True)
class NovelEntry:
    novel_id: str
    rank: int
    title: str = ""
    author: str = ""
    user_id: str = ""
    tags: tuple[str, ...] = ()
    text: str = ""

    @property
    def url(self) -> str:
        return NOVEL_PAGE_URL.format(novel_id=self.novel_id)


@dataclass(slots=True)
class IllustEntry:
    illust_id: str
    rank: int
    title: str = ""
    author: str = ""
    user_id: str = ""
    tags: tuple[str, ...] = ()
    image_url: str = ""

    @property
    def url(self) -> str:
        return ILLUST_PAGE_URL.format(illust_id=self.illust_id)


@register(
        "astrbot_plugin_pixiv_novel_r18",
    "aki",
    "每天定时抓取 Pixiv R18 小说日榜前 50 并打包为 txt 推送。",
    "1.0.0",
)
class PixivNovelR18Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._data_dir = self._resolve_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._start_scheduler()

    def _start_scheduler(self) -> None:
        try:
            self._task = asyncio.create_task(self._scheduler_loop())
        except RuntimeError:
            logger.warning("Pixiv R18 小说日榜定时任务暂未启动：当前没有运行中的事件循环。")

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @filter.command_group("pixiv_r18_subscribe")
    def pixiv_r18_subscribe(self):
        pass

    @pixiv_r18_subscribe.command("add")
    async def subscribe_add(self, event: AstrMessageEvent):
        """订阅当前群或会话的 Pixiv R18 小说日榜推送。"""
        event.stop_event()
        origins = self._get_targets()
        if event.unified_msg_origin not in origins:
            origins.append(event.unified_msg_origin)
            self._set_targets(origins)
        yield event.plain_result("已订阅当前会话的 Pixiv R18 小说日榜推送。")

    @pixiv_r18_subscribe.command("remove")
    async def subscribe_remove(self, event: AstrMessageEvent):
        """取消当前群或会话的 Pixiv R18 小说日榜推送。"""
        event.stop_event()
        origins = [origin for origin in self._get_targets() if origin != event.unified_msg_origin]
        self._set_targets(origins)
        yield event.plain_result("已取消当前会话的 Pixiv R18 小说日榜推送。")

    @pixiv_r18_subscribe.command("list")
    async def subscribe_list(self, event: AstrMessageEvent):
        """查看当前配置中的推送目标数量。"""
        event.stop_event()
        yield event.plain_result(f"当前已配置 {len(self._get_targets())} 个推送目标。")

    @filter.command("pixiv_r18_run")
    async def run_once(self, event: AstrMessageEvent):
        """立即抓取 Pixiv R18 小说日榜并发送到当前会话。"""
        event.stop_event()
        try:
            await self._send_info(event.unified_msg_origin)
            file_path, entries, novel_files = await self._build_ranking_txt()
            await self._send_txt(event.unified_msg_origin, file_path, entries, novel_files)
        except httpx.ConnectError as exc:
            logger.exception("Pixiv R18 小说日榜网络连接失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"抓取失败：连接 Pixiv 失败，请检查代理是否开启且 AstrBot 能访问。详情：{exc!r}")]),
            )
            return
        except Exception as exc:
            logger.exception("Pixiv R18 小说日榜手动抓取失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"抓取失败：{exc}")]),
            )
            return

    @filter.command("pixiv_r18_illust_run")
    async def run_illust_once(self, event: AstrMessageEvent):
        """立即抓取 Pixiv R18 插画日榜并发送到当前会话。"""
        event.stop_event()
        try:
            await self._send_illust_info(event.unified_msg_origin)
            entries, image_files = await self._build_illust_ranking_images()
            await self._send_illust_images(event.unified_msg_origin, entries, image_files)
        except httpx.ConnectError as exc:
            logger.exception("Pixiv R18 插画日榜网络连接失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"插画抓取失败：连接 Pixiv 失败，请检查代理是否开启且 AstrBot 能访问。详情：{exc!r}")]),
            )
            return
        except Exception as exc:
            logger.exception("Pixiv R18 插画日榜手动抓取失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"插画抓取失败：{exc}")]),
            )
            return

    @filter.command("pixiv_r18_check_cookie")
    async def check_cookie(self, event: AstrMessageEvent):
        """检查 Pixiv Cookie 是否能被 Pixiv 识别为登录态。"""
        event.stop_event()
        try:
            user_id = await self._check_login()
        except Exception as exc:
            yield event.plain_result(f"Cookie 检查失败：{exc}")
            return
        yield event.plain_result(f"Pixiv Cookie 有效，Pixiv 识别到用户 ID：{user_id}")

    @filter.command("pixiv_r18_test_forward")
    async def test_forward(self, event: AstrMessageEvent):
        """发送一个最小合并转发，用来测试 NapCat/OneBot 合并转发是否可用。"""
        event.stop_event()
        sender_uin = self._clamp_int("forward_sender_uin", 10000, 1, 9999999999)
        sender_name = str(self.config.get("forward_sender_name", "Pixiv 小说日榜") or "Pixiv 小说日榜")
        nodes = Comp.Nodes(
            [
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("合并转发测试 1/3：如果你能看到这条，说明节点 1 正常。")],
                ),
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("合并转发测试 2/3：这是一个纯文本节点，没有文件、图片或 Pixiv 内容。")],
                ),
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("合并转发测试 3/3：最小合并转发测试结束。")],
                ),
            ]
        )
        try:
            await self.context.send_message(event.unified_msg_origin, MessageChain(chain=[nodes]))
        except Exception as exc:
            logger.exception("Pixiv R18 小说日榜最小合并转发测试失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"最小合并转发测试失败：{exc}")]),
            )

    @filter.command("pixiv_r18_test_forward_file")
    async def test_forward_file(self, event: AstrMessageEvent):
        """发送一个包含 txt 文件节点的合并转发，用来测试 NapCat 转发内文件上传。"""
        event.stop_event()
        sender_uin = self._clamp_int("forward_sender_uin", 10000, 1, 9999999999)
        sender_name = str(self.config.get("forward_sender_name", "Pixiv 小说日榜") or "Pixiv 小说日榜")
        test_file = self._data_dir / "forward_file_test.txt"
        test_file.write_text(
            "这是一个合并转发内 txt 文件测试。\n"
            "如果纯文本合并转发成功，但这个失败，问题就在 NapCat 对转发内文件节点的上传处理。\n",
            encoding="utf-8",
        )
        nodes = Comp.Nodes(
            [
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("合并转发 txt 文件测试：下一条节点应该是一个 txt 文件。")],
                ),
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.File(name=test_file.name, file=str(test_file))],
                ),
            ]
        )
        try:
            await self.context.send_message(event.unified_msg_origin, MessageChain(chain=[nodes]))
        except Exception as exc:
            logger.exception("Pixiv R18 小说日榜合并转发 txt 文件测试失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"合并转发 txt 文件测试失败：{exc}")]),
            )

    @filter.command("pixiv_r18_test_nested_forward")
    async def test_nested_forward(self, event: AstrMessageEvent):
        """发送一个嵌套合并转发测试：外层合并转发中包含内层合并转发。"""
        event.stop_event()
        sender_uin = self._clamp_int("forward_sender_uin", 10000, 1, 9999999999)
        sender_name = str(self.config.get("forward_sender_name", "Pixiv 小说日榜") or "Pixiv 小说日榜")
        inner_nodes = Comp.Nodes(
            [
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("内层合并转发 1/2：如果能展开到这里，说明嵌套第一层成功。")],
                ),
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("内层合并转发 2/2：嵌套测试结束。")],
                ),
            ]
        )
        outer_nodes = Comp.Nodes(
            [
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("外层合并转发 1/3：下面会尝试嵌入一个内层合并转发。")],
                ),
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[inner_nodes],
                ),
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.Plain("外层合并转发 3/3：如果这条也能看到，说明外层发送完成。")],
                ),
            ]
        )
        try:
            await self.context.send_message(event.unified_msg_origin, MessageChain(chain=[outer_nodes]))
        except Exception as exc:
            logger.exception("Pixiv R18 嵌套合并转发测试失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"嵌套合并转发测试失败：{exc}")]),
            )

    @filter.command("pixiv_r18_test_10")
    async def test_forward_10_novels(self, event: AstrMessageEvent):
        """抓取并发送前 10 篇小说标题和 txt 文件，用来测试真实 Pixiv 文件合并转发。"""
        event.stop_event()
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain("开始测试：抓取 Pixiv R18 日榜前 10 篇，并发送标题 + txt 文件合并转发。")]),
            )
            file_path, entries, novel_files = await self._build_ranking_txt()
            await self._send_novel_file_forward(
                event.unified_msg_origin,
                file_path,
                entries[:10],
                novel_files[:10],
                "Pixiv R18 日榜前 10 篇真实文件测试",
            )
        except Exception as exc:
            logger.exception("Pixiv R18 小说日榜前 10 篇真实文件合并转发测试失败")
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Comp.Plain(f"前 10 篇合并转发测试失败：{exc}")]),
            )

    async def _scheduler_loop(self) -> None:
        while True:
            wait_seconds = self._seconds_until_next_run()
            await asyncio.sleep(wait_seconds)
            if not self._config_bool("enabled", True):
                continue
            targets = self._get_targets()
            if not targets:
                logger.info("Pixiv R18 小说日榜没有配置推送目标，跳过本次任务。")
                continue
            try:
                file_path, entries, novel_files = await self._build_ranking_txt()
            except Exception:
                logger.exception("Pixiv R18 小说日榜定时抓取失败")
                continue
            for target in targets:
                try:
                    await self.context.send_message(
                        target,
                        MessageChain(chain=[Comp.Plain(self._info_text())]),
                    )
                    await self._send_txt(target, file_path, entries, novel_files)
                except Exception:
                    logger.exception("Pixiv R18 小说日榜发送失败：%s", target)

    def _seconds_until_next_run(self) -> float:
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        now = datetime.now(tz)
        hour = self._schedule_hour()
        minute = self._clamp_int("schedule_minute", 0, 0, 59)
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return max((next_run - now).total_seconds(), 1.0)

    def _info_text(self) -> str:
        return "开始抓取 Pixiv R18 小说日榜，完成后会发送包含分篇 txt 的合并转发。"

    def _illust_info_text(self) -> str:
        return "开始抓取 Pixiv R18 插画日榜，完成后会发送 PDF 和 zip 图片合集。"

    async def _send_info(self, unified_msg_origin: str) -> None:
        await self.context.send_message(
            unified_msg_origin,
            MessageChain(chain=[Comp.Plain(self._info_text())]),
        )

    async def _send_illust_info(self, unified_msg_origin: str) -> None:
        await self.context.send_message(
            unified_msg_origin,
            MessageChain(chain=[Comp.Plain(self._illust_info_text())]),
        )

    async def _build_ranking_txt(self) -> tuple[Path, list[NovelEntry], list[Path]]:
        async with self._run_lock:
            entries = await self._fetch_ranking_entries()
            if not entries:
                raise RuntimeError("没有从 Pixiv 排行页解析到小说条目，请检查 Cookie 或 Pixiv 页面结构。")

            semaphore = asyncio.Semaphore(self._clamp_int("max_concurrency", 4, 1, 10))

            async def fill_detail(entry: NovelEntry) -> NovelEntry:
                async with semaphore:
                    try:
                        return await self._fetch_novel_detail(entry)
                    except Exception as exc:
                        logger.warning("抓取小说详情失败 id=%s: %s", entry.novel_id, exc)
                        return entry

            detailed_entries = await asyncio.gather(*(fill_detail(entry) for entry in entries))
            file_path = self._write_txt(detailed_entries)
            novel_files = self._write_novel_txts(detailed_entries)
            return file_path, detailed_entries, novel_files

    async def _build_illust_ranking_images(self) -> tuple[list[IllustEntry], list[list[Path]]]:
        async with self._run_lock:
            entries = await self._fetch_illust_ranking_entries()
            if not entries:
                raise RuntimeError("没有从 Pixiv 插画排行页解析到作品条目，请检查 Cookie、代理或 Pixiv 页面结构。")

            output_dir = self._prepare_illust_output_dir()
            semaphore = asyncio.Semaphore(self._clamp_int("max_concurrency", 4, 1, 10))

            async def download(entry: IllustEntry) -> list[Path]:
                async with semaphore:
                    try:
                        return await self._download_illust_images(entry, output_dir)
                    except Exception as exc:
                        logger.warning("下载插画失败 id=%s: %s", entry.illust_id, exc)
                        return []

            downloaded = await asyncio.gather(*(download(entry) for entry in entries))
            kept_entries: list[IllustEntry] = []
            image_groups: list[list[Path]] = []
            for entry, files in zip(entries, downloaded, strict=False):
                if not files:
                    continue
                kept_entries.append(entry)
                image_groups.append(files)
            if not image_groups:
                raise RuntimeError("插画排行解析成功，但没有成功下载任何图片。")
            return kept_entries, image_groups

    async def _fetch_ranking_entries(self) -> list[NovelEntry]:
        limit = self._clamp_int("limit", 50, 1, 100)
        mode = str(self.config.get("ranking_mode", "daily_r18") or "daily_r18")
        work_lang = str(self.config.get("work_lang", "zh") or "").strip()
        async with self._client() as client:
            collected: list[NovelEntry] = []
            seen: set[str] = set()
            for page in range(1, 4):
                params: dict[str, Any] = {"mode": mode, "p": page, "format": "json"}
                if work_lang:
                    params["work_lang"] = work_lang
                response = await client.get(
                    RANKING_URL,
                    params=params,
                    headers={"Referer": "https://www.pixiv.net"},
                )
                if response.status_code != 200:
                    logger.warning(
                        "Pixiv 小说排行 JSON 接口第 %s 页返回 %s，将尝试解析普通排行页。",
                        page,
                        response.status_code,
                    )
                    break
                page_entries = self._parse_ranking_json(response.text, limit)
                for entry in page_entries:
                    if entry.novel_id in seen:
                        continue
                    seen.add(entry.novel_id)
                    collected.append(entry)
                    if len(collected) >= limit:
                        return collected[:limit]
                if not page_entries:
                    break
            if collected:
                return collected[:limit]

            html_params: dict[str, Any] = {"mode": mode, "p": 1}
            if work_lang:
                html_params["work_lang"] = work_lang
            response = await client.get(RANKING_URL, params=html_params, headers={"Referer": "https://www.pixiv.net"})
            if response.status_code in (401, 403):
                raise RuntimeError(
                    "Pixiv 拒绝访问排行页。请确认 pixiv_cookie 是完整登录 Cookie、账号已开启 R-18 显示，"
                    "且运行 AstrBot 的服务器可以直接访问 pixiv.net。"
                )
            response.raise_for_status()
            return self._parse_ranking_html(response.text, limit)

    async def _fetch_illust_ranking_entries(self) -> list[IllustEntry]:
        limit = self._clamp_int("illust_limit", 50, 1, 100)
        mode = str(self.config.get("illust_ranking_mode", "daily_r18") or "daily_r18")
        content = str(self.config.get("illust_content", "illust") or "illust")
        async with self._client() as client:
            collected: list[IllustEntry] = []
            seen: set[str] = set()
            for page in range(1, 4):
                response = await client.get(
                    ILLUST_RANKING_URL,
                    params={"mode": mode, "content": content, "p": page, "format": "json"},
                    headers={"Referer": "https://www.pixiv.net/"},
                )
                if response.status_code != 200:
                    logger.warning(
                        "Pixiv 插画排行 JSON 接口第 %s 页返回 %s，将尝试解析普通排行页。",
                        page,
                        response.status_code,
                    )
                    break
                page_entries = self._parse_illust_ranking_json(response.text, limit)
                for entry in page_entries:
                    if entry.illust_id in seen:
                        continue
                    seen.add(entry.illust_id)
                    collected.append(entry)
                    if len(collected) >= limit:
                        return collected[:limit]
                if not page_entries:
                    break
            if collected:
                return collected[:limit]

            response = await client.get(
                ILLUST_RANKING_URL,
                params={"mode": mode, "content": content, "p": 1},
                headers={"Referer": "https://www.pixiv.net/"},
            )
            if response.status_code in (401, 403):
                raise RuntimeError(
                    "Pixiv 拒绝访问插画排行页。请确认 pixiv_cookie 是完整登录 Cookie、账号已开启 R-18 显示，"
                    "且运行 AstrBot 的服务器可以访问 pixiv.net。"
                )
            response.raise_for_status()
            return self._parse_illust_ranking_html(response.text, limit)

    async def _fetch_novel_detail(self, entry: NovelEntry) -> NovelEntry:
        async with self._client() as client:
            response = await client.get(
                NOVEL_AJAX_URL.format(novel_id=entry.novel_id),
                headers={"Referer": entry.url, "x-user-id": entry.user_id or ""},
            )
            if response.status_code == 200:
                detailed = self._parse_novel_ajax(response.text, entry)
                if detailed.text:
                    return detailed

            page = await client.get(entry.url, headers={"Referer": "https://www.pixiv.net/"})
            page.raise_for_status()
            return self._parse_novel_page(page.text, entry)

    async def _download_illust_images(self, entry: IllustEntry, output_dir: Path) -> list[Path]:
        async with self._client() as client:
            image_urls = await self._fetch_illust_image_urls(client, entry)
            image_paths: list[Path] = []
            base_name = self._safe_filename(f"{entry.rank:02d}_{entry.title or entry.illust_id}_{entry.author or 'unknown'}")
            for page_index, image_url in enumerate(image_urls, start=1):
                response = await client.get(
                    image_url,
                    headers={
                        "Referer": entry.url,
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    },
                )
                response.raise_for_status()
                ext = self._image_extension(image_url, response.headers.get("content-type", ""))
                suffix = f"_p{page_index:02d}" if len(image_urls) > 1 else ""
                image_path = output_dir / f"{base_name}{suffix}{ext}"
                image_path.write_bytes(response.content)
                image_paths.append(image_path)
            return image_paths

    async def _fetch_illust_image_urls(self, client: httpx.AsyncClient, entry: IllustEntry) -> list[str]:
        response = await client.get(ILLUST_PAGES_URL.format(illust_id=entry.illust_id), headers={"Referer": entry.url})
        if response.status_code == 200:
            data = response.json()
            body = data.get("body") or []
            image_urls: list[str] = []
            image_size = str(self.config.get("illust_image_size", "regular") or "regular")
            for page in body if isinstance(body, list) else []:
                urls = page.get("urls") if isinstance(page, dict) else {}
                if isinstance(urls, dict):
                    for key in (image_size, "regular", "original", "small", "thumb_mini"):
                        url = urls.get(key)
                        if url:
                            image_urls.append(str(url))
                            break
            if image_urls:
                return image_urls
        if entry.image_url:
            return [entry.image_url]
        raise RuntimeError(f"没有获取到插画图片地址：{entry.illust_id}")

    def _client(self) -> httpx.AsyncClient:
        cookie = self._normalize_cookie(str(self.config.get("pixiv_cookie", "") or ""))
        if not cookie:
            raise RuntimeError("尚未配置 pixiv_cookie，无法访问 R18 排行和小说正文。")
        return httpx.AsyncClient(
            timeout=float(self.config.get("request_timeout", 25.0) or 25.0),
            follow_redirects=True,
            headers={
                "User-Agent": str(self.config.get("user_agent", "") or ""),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Cookie": cookie,
            },
        )

    async def _check_login(self) -> str:
        async with self._client() as client:
            response = await client.get("https://www.pixiv.net/", follow_redirects=False)
            user_id = (
                response.headers.get("x-userid")
                or response.headers.get("X-UserId")
                or response.headers.get("X-Userid")
            )
            if user_id:
                return user_id

            response = await client.get("https://www.pixiv.net/")
            text = response.text
            if response.status_code in (401, 403):
                raise RuntimeError(f"Pixiv 返回 {response.status_code}，Cookie 未被接受。")
            if (
                "logout.php" in text
                or "pixiv.user.loggedIn = true" in text
                or "_setCustomVar', 1, 'login', 'yes'" in text
                or "dataLayer = [{ login: 'yes'" in text
            ):
                return "已登录"
        raise RuntimeError("Pixiv 没有识别到登录态，请重新复制完整 Cookie 或 PHPSESSID。")

    def _parse_ranking_json(self, text: str, limit: int) -> list[NovelEntry]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        contents = data.get("contents") or data.get("body", {}).get("contents") or []
        entries: list[NovelEntry] = []
        for index, item in enumerate(contents[:limit], start=1):
            novel_id = str(item.get("illust_id") or item.get("novel_id") or item.get("id") or "")
            if not novel_id:
                continue
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            entries.append(
                NovelEntry(
                    novel_id=novel_id,
                    rank=int(item.get("rank") or index),
                    title=str(item.get("title") or ""),
                    author=str(item.get("user_name") or item.get("userName") or ""),
                    user_id=str(item.get("user_id") or item.get("userId") or ""),
                    tags=tuple(str(tag) for tag in tags),
                )
            )
        return entries

    def _parse_ranking_html(self, text: str, limit: int) -> list[NovelEntry]:
        soup = BeautifulSoup(text, "html.parser")
        seen: set[str] = set()
        entries: list[NovelEntry] = []
        for link in soup.select('a[href*="/novel/show.php?id="], a[href*="/novel/"]'):
            href = link.get("href") or ""
            match = re.search(r"(?:id=|/novel/)(\d+)", href)
            if not match:
                continue
            novel_id = match.group(1)
            if novel_id in seen:
                continue
            seen.add(novel_id)
            title = link.get("title") or link.get_text(" ", strip=True)
            entries.append(NovelEntry(novel_id=novel_id, rank=len(entries) + 1, title=title))
            if len(entries) >= limit:
                break
        return entries

    def _parse_illust_ranking_json(self, text: str, limit: int) -> list[IllustEntry]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        contents = data.get("contents") or data.get("body", {}).get("contents") or []
        entries: list[IllustEntry] = []
        for index, item in enumerate(contents[:limit], start=1):
            if not isinstance(item, dict):
                continue
            illust_id = str(item.get("illust_id") or item.get("id") or "")
            if not illust_id:
                continue
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            entries.append(
                IllustEntry(
                    illust_id=illust_id,
                    rank=int(item.get("rank") or index),
                    title=str(item.get("title") or ""),
                    author=str(item.get("user_name") or item.get("userName") or ""),
                    user_id=str(item.get("user_id") or item.get("userId") or ""),
                    tags=tuple(str(tag) for tag in tags),
                    image_url=str(item.get("url") or item.get("illust_url") or ""),
                )
            )
        return entries

    def _parse_illust_ranking_html(self, text: str, limit: int) -> list[IllustEntry]:
        soup = BeautifulSoup(text, "html.parser")
        seen: set[str] = set()
        entries: list[IllustEntry] = []
        for link in soup.select('a[href*="/artworks/"], a[href*="illust_id="]'):
            href = link.get("href") or ""
            match = re.search(r"(?:/artworks/|illust_id=)(\d+)", href)
            if not match:
                continue
            illust_id = match.group(1)
            if illust_id in seen:
                continue
            seen.add(illust_id)
            title = link.get("title") or link.get_text(" ", strip=True)
            entries.append(IllustEntry(illust_id=illust_id, rank=len(entries) + 1, title=title))
            if len(entries) >= limit:
                break
        return entries

    def _parse_novel_ajax(self, text: str, entry: NovelEntry) -> NovelEntry:
        data = json.loads(text)
        body = data.get("body") or {}
        tags = body.get("tags", {}).get("tags", [])
        tag_names = tuple(str(tag.get("tag") or tag.get("name") or "") for tag in tags if isinstance(tag, dict))
        novel_text = self._find_first_text(body, ("text", "content"))
        return NovelEntry(
            novel_id=entry.novel_id,
            rank=entry.rank,
            title=str(body.get("title") or entry.title),
            author=str(body.get("userName") or body.get("user_name") or entry.author),
            user_id=str(body.get("userId") or body.get("user_id") or entry.user_id),
            tags=tag_names or entry.tags,
            text=str(novel_text or entry.text),
        )

    def _parse_novel_page(self, text: str, entry: NovelEntry) -> NovelEntry:
        soup = BeautifulSoup(text, "html.parser")
        script = soup.select_one("#__NEXT_DATA__")
        if script and script.string:
            data = json.loads(script.string)
            novel_data = self._find_novel_object(data, entry.novel_id)
            if novel_data:
                tags = novel_data.get("tags") or entry.tags
                if isinstance(tags, list):
                    tags = tuple(str(tag.get("tag") if isinstance(tag, dict) else tag) for tag in tags)
                return NovelEntry(
                    novel_id=entry.novel_id,
                    rank=entry.rank,
                    title=str(novel_data.get("title") or entry.title),
                    author=str(novel_data.get("userName") or novel_data.get("user_name") or entry.author),
                    user_id=str(novel_data.get("userId") or novel_data.get("user_id") or entry.user_id),
                    tags=tuple(tags),
                    text=str(self._find_first_text(novel_data, ("content", "text")) or entry.text),
                )
        return entry

    def _find_novel_object(self, value: Any, novel_id: str) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if str(value.get("id") or value.get("novelId") or "") == novel_id:
                return value
            preload = value.get("preload")
            if isinstance(preload, dict):
                novels = preload.get("novel")
                if isinstance(novels, dict) and isinstance(novels.get(novel_id), dict):
                    return novels[novel_id]
            for item in value.values():
                found = self._find_novel_object(item, novel_id)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_novel_object(item, novel_id)
                if found:
                    return found
        return None

    def _find_first_text(self, value: Any, keys: tuple[str, ...]) -> str:
        if isinstance(value, dict):
            for key in keys:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item
            for item in value.values():
                found = self._find_first_text(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_first_text(item, keys)
                if found:
                    return found
        return ""

    def _write_txt(self, entries: list[NovelEntry]) -> Path:
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        today = datetime.now(tz).strftime("%Y-%m-%d")
        file_path = self._data_dir / f"pixiv_daily_r18_novels_{today}.txt"
        lines = [
            f"Pixiv 小说 R18 日榜 Top {len(entries)}",
            f"生成时间：{datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"排行模式：{self.config.get('ranking_mode', 'daily_r18')}",
            "",
            "目录",
        ]
        for entry in entries:
            lines.append(f"{entry.rank:02d}. {entry.title or '(无标题)'} / {entry.author or '未知作者'}")
        lines.append("\n" + "=" * 60 + "\n")
        for entry in entries:
            lines.extend(
                [
                    f"#{entry.rank:02d} {entry.title or '(无标题)'}",
                    f"作者：{entry.author or '未知作者'}",
                    f"链接：{entry.url}",
                    f"标签：{', '.join(tag for tag in entry.tags if tag) or '无'}",
                    "",
                    entry.text.strip() or "[未能获取正文，请检查 Pixiv Cookie 权限或页面结构]",
                    "",
                    "=" * 60,
                    "",
                ]
            )
        file_path.write_text("\n".join(lines), encoding="utf-8")
        return file_path

    def _write_novel_txts(self, entries: list[NovelEntry]) -> list[Path]:
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        today = datetime.now(tz).strftime("%Y-%m-%d")
        output_dir = self._data_dir / f"pixiv_daily_r18_novels_{today}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        files: list[Path] = []
        for entry in entries:
            name = self._safe_filename(f"{entry.rank:02d}_{entry.title or entry.novel_id}_{entry.author or 'unknown'}")
            file_path = output_dir / f"{name}.txt"
            content = "\n".join(
                [
                    f"#{entry.rank:02d} {entry.title or '(无标题)'}",
                    f"作者：{entry.author or '未知作者'}",
                    f"链接：{entry.url}",
                    f"标签：{', '.join(tag for tag in entry.tags if tag) or '无'}",
                    "",
                    entry.text.strip() or "[未能获取正文，请检查 Pixiv Cookie 权限或页面结构]",
                    "",
                ]
            )
            file_path.write_text(content, encoding="utf-8")
            files.append(file_path)
        return files

    def _prepare_illust_output_dir(self) -> Path:
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        today = datetime.now(tz).strftime("%Y-%m-%d")
        output_dir = self._data_dir / f"pixiv_daily_r18_illusts_{today}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _write_illust_zip(self, entries: list[IllustEntry], image_groups: list[list[Path]]) -> Path:
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        today = datetime.now(tz).strftime("%Y-%m-%d")
        zip_path = self._data_dir / f"pixiv_daily_r18_illusts_{today}.zip"
        if zip_path.exists():
            zip_path.unlink()

        manifest_lines = [
            f"Pixiv 插画 R18 日榜 Top {len(entries)}",
            f"生成时间：{datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"排行模式：{self.config.get('illust_ranking_mode', 'daily_r18')}",
            f"内容类型：{self.config.get('illust_content', 'illust')}",
            f"图片规格：{self.config.get('illust_image_size', 'regular')}",
            "",
            "目录",
        ]
        for entry, files in zip(entries, image_groups, strict=False):
            manifest_lines.extend(
                [
                    f"{entry.rank:02d}. {entry.title or '(无标题)'} / {entry.author or '未知作者'}",
                    f"    链接：{entry.url}",
                    f"    文件：{', '.join(image_file.name for image_file in files)}",
                ]
            )

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.txt", "\n".join(manifest_lines))
            for image_file in self._flatten_image_groups(image_groups):
                archive.write(image_file, arcname=image_file.name)
        return zip_path

    def _write_illust_pdf(self, entries: list[IllustEntry], image_groups: list[list[Path]]) -> Path:
        tz = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"))
        today = datetime.now(tz).strftime("%Y-%m-%d")
        pdf_path = self._data_dir / f"pixiv_daily_r18_illusts_{today}.pdf"
        if pdf_path.exists():
            pdf_path.unlink()

        pdf_pages: list[PILImage.Image] = []
        try:
            for image_file in self._flatten_image_groups(image_groups):
                with PILImage.open(image_file) as image:
                    page = ImageOps.exif_transpose(image)
                    if page.mode in ("RGBA", "LA", "P"):
                        background = PILImage.new("RGB", page.size, "white")
                        if page.mode == "P":
                            page = page.convert("RGBA")
                        background.paste(page, mask=page.getchannel("A") if page.mode in ("RGBA", "LA") else None)
                        page = background
                    else:
                        page = page.convert("RGB")
                    pdf_pages.append(page.copy())
            if not pdf_pages:
                raise RuntimeError("没有可写入 PDF 的插画图片。")
            first_page, rest_pages = pdf_pages[0], pdf_pages[1:]
            first_page.save(pdf_path, "PDF", save_all=True, append_images=rest_pages)
            return pdf_path
        finally:
            for page in pdf_pages:
                page.close()

    async def _send_txt(
        self,
        unified_msg_origin: str,
        file_path: Path,
        entries: list[NovelEntry],
        novel_files: list[Path],
    ) -> None:
        summary = (
            f"Pixiv 小说 R18 日榜 Top {len(entries)}\n"
            f"作品语言：{str(self.config.get('work_lang', 'zh') or '全部')}\n"
            f"本地总表：{file_path.name}\n"
            f"合并转发内含 {len(entries)} 篇分开的小说正文 txt。\n"
            f"榜首：{entries[0].title if entries else '无'}"
        )
        if self._config_bool("send_as_forward", True):
            batch_size = self._clamp_int("forward_batch_size", 10, 1, 25)
            batches = self._split_forward_batches(entries, novel_files, batch_size)
            for batch_index, (batch_entries, batch_files) in enumerate(batches, start=1):
                try:
                    await self._send_novel_file_forward(
                        unified_msg_origin,
                        file_path,
                        batch_entries,
                        batch_files,
                        self._format_batch_summary(summary, batch_index, len(batches), batch_entries),
                    )
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    logger.exception("Pixiv R18 小说日榜合并转发第 %s/%s 包发送失败", batch_index, len(batches))
                    await self.context.send_message(
                        unified_msg_origin,
                        MessageChain(chain=[Comp.Plain(f"第 {batch_index}/{len(batches)} 个合并转发发送失败：{exc}")]),
                    )
            return
        await self.context.send_message(unified_msg_origin, MessageChain(chain=[Comp.Plain(summary)]))

    async def _send_novel_file_forward(
        self,
        unified_msg_origin: str,
        file_path: Path,
        entries: list[NovelEntry],
        novel_files: list[Path],
        title: str,
    ) -> None:
        sender_uin = self._clamp_int("forward_sender_uin", 10000, 1, 9999999999)
        sender_name = str(self.config.get("forward_sender_name", "Pixiv 小说日榜") or "Pixiv 小说日榜")
        summary = (
            f"{title}\n"
            f"本地总表：{file_path.name}\n"
            f"本次测试数量：{len(entries)}\n"
            "结构：每篇小说 1 条标题节点 + 1 个 txt 文件节点"
        )
        nodes: list[Comp.Node] = [
            Comp.Node(
                uin=sender_uin,
                name=sender_name,
                content=[Comp.Plain(summary + "\n\n" + self._build_catalog(entries))],
            )
        ]
        nodes.extend(self._build_novel_file_nodes(entries, novel_files, sender_uin, sender_name))
        await self.context.send_message(unified_msg_origin, MessageChain(chain=[Comp.Nodes(nodes)]))

    async def _send_illust_images(
        self,
        unified_msg_origin: str,
        entries: list[IllustEntry],
        image_groups: list[list[Path]],
    ) -> None:
        if self._config_bool("illust_send_as_zip", True):
            pdf_path = self._write_illust_pdf(entries, image_groups)
            zip_path = self._write_illust_zip(entries, image_groups)
            await self._send_illust_archives(unified_msg_origin, pdf_path, zip_path)
            return

        summary = (
            f"Pixiv 插画 R18 日榜 Top {len(entries)}\n"
            f"内容类型：{self.config.get('illust_content', 'illust')}\n"
            f"图片规格：{self.config.get('illust_image_size', 'regular')}\n"
            f"榜首：{entries[0].title if entries else '无'}"
        )
        batch_size = self._clamp_int("illust_forward_batch_size", 5, 1, 25)
        batches = self._split_illust_batches(entries, image_groups, batch_size)
        for batch_index, (batch_entries, batch_files) in enumerate(batches, start=1):
            try:
                await self._send_illust_image_forward(
                    unified_msg_origin,
                    batch_entries,
                    batch_files,
                    self._format_illust_batch_summary(summary, batch_index, len(batches), batch_entries),
                )
                await asyncio.sleep(1.0)
            except Exception as exc:
                logger.exception("Pixiv R18 插画日榜合并转发第 %s/%s 包发送失败", batch_index, len(batches))
                await self.context.send_message(
                    unified_msg_origin,
                    MessageChain(
                        chain=[
                            Comp.Plain(
                                f"第 {batch_index}/{len(batches)} 个插画合并转发发送失败：{exc}\n"
                                "如果偶发失败，建议把 illust_forward_batch_size 调小，或稍后重试。"
                            )
                        ]
                    ),
                )

    async def _send_illust_image_forward(
        self,
        unified_msg_origin: str,
        entries: list[IllustEntry],
        image_groups: list[list[Path]],
        title: str,
    ) -> None:
        sender_uin = self._clamp_int("forward_sender_uin", 10000, 1, 9999999999)
        sender_name = str(self.config.get("forward_sender_name", "Pixiv 小说日榜") or "Pixiv 小说日榜")
        nodes: list[Comp.Node] = [
            Comp.Node(
                uin=sender_uin,
                name=sender_name,
                content=[Comp.Plain(title + "\n\n" + self._build_illust_catalog(entries))],
            )
        ]
        for entry, files in zip(entries, image_groups, strict=False):
            nodes.append(
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[
                        Comp.Plain(
                            "\n".join(
                                [
                                    f"#{entry.rank:02d} {entry.title or '(无标题)'}",
                                    f"作者：{entry.author or '未知作者'}",
                                    f"链接：{entry.url}",
                                    f"图片：{', '.join(image_file.name for image_file in files)}",
                                ]
                            )
                        )
                    ],
                )
            )
            for image_file in files:
                nodes.append(
                    Comp.Node(
                        uin=sender_uin,
                        name=sender_name,
                        content=[Comp.Image.fromFileSystem(str(image_file))],
                    )
                )
        await self.context.send_message(unified_msg_origin, MessageChain(chain=[Comp.Nodes(nodes)]))

    async def _send_illust_archives(
        self,
        unified_msg_origin: str,
        pdf_path: Path,
        zip_path: Path,
    ) -> None:
        await self.context.send_message(
            unified_msg_origin,
            MessageChain(chain=[Comp.File(name=pdf_path.name, file=str(pdf_path))]),
        )
        await self.context.send_message(
            unified_msg_origin,
            MessageChain(chain=[Comp.File(name=zip_path.name, file=str(zip_path))]),
        )

    def _build_novel_file_nodes(
        self,
        entries: list[NovelEntry],
        novel_files: list[Path],
        sender_uin: int,
        sender_name: str,
    ) -> list[Comp.Node]:
        nodes: list[Comp.Node] = []
        for entry, novel_file in zip(entries, novel_files, strict=False):
            nodes.append(
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[
                        Comp.Plain(
                            "\n".join(
                                [
                                    f"#{entry.rank:02d} {entry.title or '(无标题)'}",
                                    f"作者：{entry.author or '未知作者'}",
                                    f"链接：{entry.url}",
                                    f"文件：{novel_file.name}",
                                ]
                            )
                        )
                    ],
                )
            )
            nodes.append(
                Comp.Node(
                    uin=sender_uin,
                    name=sender_name,
                    content=[Comp.File(name=novel_file.name, file=str(novel_file))],
                )
            )
        return nodes

    def _split_forward_batches(
        self,
        entries: list[NovelEntry],
        novel_files: list[Path],
        batch_size: int,
    ) -> list[tuple[list[NovelEntry], list[Path]]]:
        batches: list[tuple[list[NovelEntry], list[Path]]] = []
        for start in range(0, len(entries), batch_size):
            batches.append((entries[start : start + batch_size], novel_files[start : start + batch_size]))
        return batches

    def _split_illust_batches(
        self,
        entries: list[IllustEntry],
        image_groups: list[list[Path]],
        batch_size: int,
    ) -> list[tuple[list[IllustEntry], list[list[Path]]]]:
        batches: list[tuple[list[IllustEntry], list[list[Path]]]] = []
        for start in range(0, len(entries), batch_size):
            batches.append((entries[start : start + batch_size], image_groups[start : start + batch_size]))
        return batches

    def _flatten_image_groups(self, image_groups: list[list[Path]]) -> list[Path]:
        return [image_file for files in image_groups for image_file in files]

    def _format_batch_summary(
        self,
        summary: str,
        batch_index: int,
        batch_count: int,
        entries: list[NovelEntry],
    ) -> str:
        if not entries:
            return summary
        return "\n".join(
            [
                summary,
                "",
                f"分包：{batch_index}/{batch_count}",
                f"范围：#{entries[0].rank:02d} - #{entries[-1].rank:02d}",
            ]
        )

    def _format_illust_batch_summary(
        self,
        summary: str,
        batch_index: int,
        batch_count: int,
        entries: list[IllustEntry],
    ) -> str:
        if not entries:
            return summary
        return "\n".join(
            [
                summary,
                "",
                f"分包：{batch_index}/{batch_count}",
                f"范围：#{entries[0].rank:02d} - #{entries[-1].rank:02d}",
            ]
        )

    def _build_forward_text_nodes(
        self,
        entries: list[NovelEntry],
        novel_files: list[Path],
        sender_uin: int,
        sender_name: str,
        summary: str,
    ) -> list[Comp.Node]:
        nodes = [
            Comp.Node(
                uin=sender_uin,
                name=sender_name,
                content=[Comp.Plain(summary + "\n\n" + self._build_catalog(entries))],
            )
        ]
        for entry, novel_file in zip(entries, novel_files, strict=False):
            body = novel_file.read_text(encoding="utf-8")
            for index, chunk in enumerate(self._chunk_text(body, 3500), start=1):
                suffix = "" if index == 1 else f" - part {index}"
                nodes.append(
                    Comp.Node(
                        uin=sender_uin,
                        name=sender_name,
                        content=[Comp.Plain(f"{novel_file.name}{suffix}\n\n{chunk}")],
                    )
                )
        return nodes

    def _build_forward_nodes(
        self,
        entries: list[NovelEntry],
        sender_uin: int,
        sender_name: str,
        summary: str,
    ) -> list[Comp.Node]:
        nodes = [
            Comp.Node(
                uin=sender_uin,
                name=sender_name,
                content=[Comp.Plain(summary + "\n\n" + self._build_catalog(entries))],
            )
        ]
        for entry in entries:
            text = self._format_entry(entry)
            for index, chunk in enumerate(self._chunk_text(text, 12000), start=1):
                suffix = "" if index == 1 else f"（续 {index}）"
                nodes.append(
                    Comp.Node(
                        uin=sender_uin,
                        name=sender_name,
                        content=[Comp.Plain(f"#{entry.rank:02d} {entry.title or '(无标题)'}{suffix}\n\n{chunk}")],
                    )
                )
        return nodes

    def _build_catalog(self, entries: list[NovelEntry]) -> str:
        lines = ["目录"]
        for entry in entries:
            lines.append(f"{entry.rank:02d}. {entry.title or '(无标题)'} / {entry.author or '未知作者'}")
        return "\n".join(lines)

    def _build_illust_catalog(self, entries: list[IllustEntry]) -> str:
        lines = ["目录"]
        for entry in entries:
            lines.append(f"{entry.rank:02d}. {entry.title or '(无标题)'} / {entry.author or '未知作者'}")
        return "\n".join(lines)

    def _format_entry(self, entry: NovelEntry) -> str:
        return "\n".join(
            [
                f"作者：{entry.author or '未知作者'}",
                f"链接：{entry.url}",
                f"标签：{', '.join(tag for tag in entry.tags if tag) or '无'}",
                "",
                entry.text.strip() or "[未能获取正文，请检查 Pixiv Cookie 权限或页面结构]",
            ]
        )

    def _chunk_text(self, text: str, size: int) -> list[str]:
        if len(text) <= size:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            newline = text.rfind("\n", start, end)
            if newline > start + size // 2:
                end = newline
            chunks.append(text[start:end].strip())
            start = end
        return [chunk for chunk in chunks if chunk]

    def _safe_filename(self, name: str) -> str:
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        safe = re.sub(r"\s+", " ", safe).strip(" .")
        return (safe or "novel")[:120]

    def _image_extension(self, url: str, content_type: str) -> str:
        content_type = content_type.lower()
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
        match = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:$|[?&])", url, re.IGNORECASE)
        if match:
            ext = match.group(1).lower()
            return ".jpg" if ext == "jpeg" else f".{ext}"
        return ".jpg"

    def _resolve_data_dir(self) -> Path:
        cwd_data = Path.cwd() / "data" / "pixiv_novel_r18"
        if cwd_data.parent.exists() or Path.cwd().name.lower() == "astrbot":
            return cwd_data
        return Path(__file__).resolve().parent / "data"

    def _get_targets(self) -> list[str]:
        targets = self.config.get("target_unified_msg_origins", []) or []
        return [str(target) for target in targets if str(target).strip()]

    def _set_targets(self, targets: list[str]) -> None:
        self.config["target_unified_msg_origins"] = targets
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return bool(value)

    def _clamp_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, minimum), maximum)

    def _schedule_hour(self) -> int:
        try:
            hour = int(self.config.get("schedule_hour", 0))
        except (TypeError, ValueError):
            hour = 0
        if hour == 24:
            return 0
        return min(max(hour, 0), 23)

    def _normalize_cookie(self, raw_cookie: str) -> str:
        cookie = raw_cookie.strip()
        if not cookie:
            return ""
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        if "=" not in cookie:
            return f"PHPSESSID={cookie}"

        pairs: list[str] = []
        for item in cookie.split(";"):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name:
                pairs.append(f"{name}={value}")
        return "; ".join(pairs)
