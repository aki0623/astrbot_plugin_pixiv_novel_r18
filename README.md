# Pixiv R18 小说日榜推送

AstrBot 插件：每天 00:00 抓取 Pixiv 小说 R18 日榜，默认筛选中文作品，将每篇小说保存为独立 txt，并按每 10 篇一个合并转发推送到订阅群。也支持手动抓取 Pixiv R18 插画日榜前 50，并按每 5 个作品一个合并转发发送标题和图片。

## 使用

1. 安装依赖：AstrBot 会读取 `requirements.txt`。
2. 在插件配置中填写 `pixiv_cookie`，需要能访问 R18 内容的 Pixiv 登录 Cookie。可以填完整 `Cookie:` 串、`PHPSESSID=...`，也可以只填 PHPSESSID 的值。
3. 在要接收推送的群中发送：

```text
/pixiv_r18_subscribe add
```

4. 立即测试一次：

```text
/pixiv_r18_run
```

如果 AstrBot 配置了全局唤醒词，例如 `koha`，也可以使用 `koha pixiv_r18_run`。

抓取插画 R18 日榜：

```text
/pixiv_r18_illust_run
```

## 指令

- `/pixiv_r18_subscribe add`：订阅当前会话。
- `/pixiv_r18_subscribe remove`：取消订阅当前会话。
- `/pixiv_r18_subscribe list`：查看订阅数量。
- `/pixiv_r18_run`：立即抓取并发送到当前会话。
- `/pixiv_r18_illust_run`：立即抓取 Pixiv R18 插画日榜前 50，下载图片并发送到当前会话。
- `/pixiv_r18_check_cookie`：检查 Cookie 是否能被 Pixiv 识别为登录态。
- `/pixiv_r18_test_forward`：发送最小合并转发测试。
- `/pixiv_r18_test_forward_file`：发送包含 txt 文件的合并转发测试。
- `/pixiv_r18_test_10`：抓取前 10 篇并发送标题 + txt 文件测试。

## 主要配置

- `pixiv_cookie`：Pixiv 登录 Cookie，至少包含 `PHPSESSID`。
- `work_lang`：作品语言筛选，默认 `zh`。留空则不筛选语言。
- `limit`：抓取数量，默认 `50`。
- `forward_batch_size`：每个合并转发包含的小说篇数，默认 `10`。
- `illust_limit`：插画抓取数量，默认 `50`。
- `illust_image_size`：插画下载规格，默认 `regular`。`original` 体积更大，更容易触发合并转发失败。
- `illust_forward_batch_size`：每个插画合并转发包含的作品数，默认 `5`。图片合并转发偶发失败时继续调小。
- `schedule_hour` / `schedule_minute`：每日定时推送时间，默认 `00:00`。

## 注意

- Pixiv R18 内容必须登录后访问，账号需要开启 R-18 显示。
- 合并转发消息目前主要适配 OneBot v11 / aiocqhttp / NapCat。
- 不要把真实 Cookie 提交到仓库或公开分享。
