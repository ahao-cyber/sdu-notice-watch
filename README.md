# 山大通知监控

每天定时抓取山东大学各官网的通知公告，去重、按重要性分级，早上 7:30 发一封邮件日报。

## 它能做什么

- 抓 **16 个源**：本科生院、计算机学院（官网 + 学工网）、青岛校区、学生在线、就业中心、图书馆、新闻网、研究生之家、青春山大、青岛校区学生工作网
- **去重**：按标题+链接指纹记住已见条目，只报新的（state.json）
- **分级**：★必看（选课/考试/四六级/推免/评奖评优/奖学金/家庭经济困难认定/转专业…）、·值得看（竞赛/招募/讲座/实习/交换…）、·其他
- **日报**：`daily/YYYY-MM-DD.md`，邮件正文同内容（HTML）
- **绝不静默**：某个源抓取失败或解析出 0 条，会在日报里单列"异常"并把 GitHub Actions 跑成红色（有邮件提醒）

## 一个重要限制：两个源只能在校内网抓

`青春山大·重要通知` 和 `青岛校区学生工作网·学团通知` 有 **VSB 栏目 IP 权限**，
非校园网 IP（含任何云服务器、家里宽带、4G）都会被跳到 `ipauth.htm`（"您没有访问当前栏目的权限"）。
实测记录：境外多国节点均 302 到该页，其余 14 个源 200 正常。

因此分两路：

| 路由 | 跑在哪 | 覆盖 |
|---|---|---|
| 云端 | GitHub Actions（每天 07:30） | 14 个校外可达源 |
| 本机 | 你的电脑（在校内网时手动跑或定时跑） | 那 2 个受限源，输出 `daily/YYYY-MM-DD-local.md` 并单独发一封邮件 |

## 本机怎么用

```bash
# 体检：看每个源能不能抓、能解析出几条
python watch.py --check

# 建基线：把当前所有条目记为已见（首次部署时跑一次，之后不会把历史当新条目）
python watch.py --baseline

# 正常跑（本机在校内网时也把受限源补上）
python watch.py --send

# 只跑校内权限源（本机模式），并单独发一封
python watch.py --local --send
```

也可以双击 `run-local.cmd`。

## 日报怎么送到你手上（三条通道，可叠加）

主渠道**不需要任何密钥**：

| 通道 | 需要什么 | 说明 |
|---|---|---|
| **GitHub 通知**（默认） | 什么都不用填 | 每天把日报发成仓库里的一条 Issue，并在正文 @ 仓库所有者 → GitHub 给你发邮件提醒；手机打开仓库 Issues 页也能看 |
| 微信推送（可选） | Server酱 SendKey | 填 `mail.local.json` 的 `serverchan_key`，同时推到微信 |
| 邮件（可选） | QQ邮箱 SMTP 授权码 | 见下 |

邮件那条要不要用随意 —— 它只是让日报以"真正的邮件"出现在你邮箱里，而不是 GitHub 的通知邮件。

### 可选：配邮件

1. QQ邮箱 → 右上角头像 → 设置 → 账号与安全 → 安全设置 → 开启服务 → 生成**授权码**（16 位，只显示一次）
2. 复制 `mail.local.example.json` 为 `mail.local.json`，填 `user`（邮箱）和 `pass`（授权码）
   （这个文件已在 .gitignore 里，不会进仓库）
3. 测试：`python watch.py --send`

云端用 GitHub Secrets（SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_TO / SERVERCHAN_KEY）。

## 加源 / 改关键词

编辑 `sources.json`：

- `sources[]`：加一条 `{id, name, url, parser, priority, restricted}`；
  `parser` 取值见 `watch.py` 里的 `PARSERS`（bkjx_index / bkjx_list / cs_title_dates /
  vsb_li_title_date / vsb_li_date_title / news_h3_em / news_p_span / qdxq / online_home /
  date_then_title / lib_article / youth_list / onlineqd_home / ygb）
- `tag_rules.A` / `tag_rules.B`：决定条目进"必看"还是"值得看"

改完先 `python watch.py --check` 确认解析条数正常。

## 已知坑（都踩过）

- **VSB 列表页路径不可猜**：猜的 URL 全 404，必须从首页导航里找
- **有的栏目早就停更**（新闻网通知公告停在 2025-01、学院"学院公告"停在 2025-01），
  配 `allow_empty: true` 免得天天报"解析失效"
- **VSB 列表常不写年份**（如青岛校区只给"29 09月"），按抓取年份补，若算出未来日期则回退一年
- **学工网的列表不是按日期排序的**，必须自己按日期排
- **日期在标题前还是在后，各源不一样**，这就是为什么要多个 parser
- **附件下载要带 Referer**，否则返回 3.8KB 的 HTML 壳冒充 PDF
- **GitHub Actions 跑在 UTC**，脚本内部统一按北京时间（UTC+8）算日期，否则日报日期会差一天
- GitHub 的定时任务有几分钟到几十分钟延迟，且**仓库 60 天无活动会被自动停用**——
  工作流每天提交 state.json 就是为了保持活跃；真被停了 GitHub 会发邮件，点一下就能恢复
