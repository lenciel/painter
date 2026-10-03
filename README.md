# painter

一系列从博物馆开放数据接口采集名画的爬虫，图片按 `作者名_作品名_年代.jpeg` 保存
（空格用下划线代替）。目前实现了第一个数据源：大都会艺术博物馆（The Met）。

```
William_Merritt_Chase_At_the_Seaside_1892.jpeg
Albert_Gleizes_Kubismus_1928.jpeg
```

## 安装

```bash
uv venv && uv pip install -e ".[dev]"     # 或者：pip install -e ".[dev]"
```

## 使用

```bash
painter met                                  # 抓取 Met 开放数据里全部绘画
painter met --limit 50                       # 产生 50 个结果后停止
painter met --department "European Paintings" # 只要某个部门
painter met --public-domain-only             # 只要 API 认定属于公有领域的
painter met --restricted-only                # 只要仍在版权期内的
painter met --object-id 10464 --object-id 818854   # 指定对象，绕过索引
painter met --dry-run --limit 20             # 只打印文件名，不联网
```

图片存到 `paintings/met/`，同目录下还有 `.manifest.jsonl`——一个只追加的台账，
记录每件作品的处理结果（`ok`、`no-image`、`skipped`、`error`，以及图片地址、字节数、
版权说明）。重跑会跳过已记录为下载完成的作品，所以爬取可以断点续跑；
`--overwrite` 会强制重新下载。

`--limit N` 约束的是「产生了结果」的作品数（已下载、无图片、失败）；
被公有领域筛选丢掉的作品不占额度。

公有领域筛选依据的是从 Collection API 取回的那条记录，而不是 CSV 里的那一列：
对较早的记录两者会不一致，而决定是否放出全分辨率图片的正是 API。

## 文件命名规则

| 规则 | 示例 |
| --- | --- |
| `作者_作品_年代.jpeg` | `Vincent_van_Gogh_Wheat_Field_with_Cypresses_1889.jpeg` |
| 连续空白 → `_` | `At the Seaside` → `At_the_Seaside` |
| 路径非法字符（`/ \ : * ? " < > \|`）直接丢掉 | `"King Lear," Act I` → `King_Lear_Act_I` |
| 自由文本日期只有一个年份时用它 | `ca. 1892` → `1892` |
| 跨年用机器可读的起止年 | `1865–67` → `1865-1867` |
| 作者缺失 → `unknown` | |
| 两件作品撞名 | 后者追加对象 ID：`..._1892_10464.jpeg` |

只折叠空白，重音字母和其他 Unicode 字符都保留（例如 `Bauhausbücher`）。

## Met 数据源的实现方式

* **枚举** —— `MetSource.iter_works()` 流式读取 CC0 的
  [开放数据](https://github.com/metmuseum/openaccess)（`MetObjects.csv`，约 320 MB，
  缓存在 `$PAINTER_CACHE` 或 `~/.cache/painter`），满足下面任一条件的行会被保留：
  * `Classification` 等于 `Paintings`（9,005 行），或
  * `Classification` 为空 **且** `Object Name` 恰好是 `Painting`（1,231 行）。

  第二条规则是必需的，不是修饰：馆方开放数据把**美国馆（The American Wing）所有画作**
  的 `Classification` 都留空——包括对象 10464《At the Seaside》——但仍把它们标注为
  `Object Name: Painting`。没有这条规则，这些作品会被静默漏掉。精确匹配同时把
  相近但不同的类型排除在外（`Painting, miniature`、`Paintings-Panels`、
  `Bark-Paintings` 等）。Collection API 自己的 `classification` 字段有同样的缺口，
  所以也不能拿 API 当筛选器。当前数据集共 **10,236 件绘画**。
* **命名** —— 随后逐件请求 `GET /public/collection/v1/objects/{id}`，其中的
  `artistDisplayName`/`title`/`objectDate` 是权威值。CSV 对多作者作品是有损的
  （用 `|` 把名字拼起来，例如 `Wang Hui|Unidentified artist`，还有标题为空的行），
  因此 CSV 只在 API 记录缺失时兜底——比如对象被下架、接口返回 404 的情况。
* **公有领域图片** —— API 的 `primaryImage` 指向全分辨率原图
  （例如对象 10464 是 3811×2288）。
* **仍在版权期内的作品** —— API 的 `primaryImage` 是空的。对象页面通过 Open Graph
  元数据暴露一张图：
  `https://collectionapi.metmuseum.org/api/collection/v1/iiif/{id}/{image}/restricted`，
  这是馆方为这类作品提供的最大尺寸（长边约 600 px；没有 `info.json`，也没有可选的
  IIIF 尺寸变体）。爬虫直接读取页面上的 `og:image`，并拒绝任何不属于该对象的图片。
* **完全没有图片** —— 少数作品页面上就不显示图片，这类会记为 `no-image`，不下载。

### 接口注意事项

* 每件作品一次元数据请求（同时拿到图片地址），版权期内的作品再多一次页面请求。
  以默认 4 req/s 计，跑完 10,236 件大约需要一小时，外加图片传输时间
  （公有领域的原图很大，单张往往好几 MB）。
* `--dry-run` 只用索引元数据打印文件名（不联网），所以少数文件名会与真实运行时
  经 API 校正后的名字不同。
* `/public/collection/v1/search` 已于 2026-10-01 下线；`/v1.1/search` 是分页的
  （`offset`/`limit`），而且只能取到前 10,000 条结果——这就是枚举改用 CSV 的原因。
* 两个域名都在 Akamai 机器人防护之后：普通 `requests`/`curl` 因为 TLS 指纹，
  请求几次就会收到 403/429。所有流量都走 `curl_cffi` 的浏览器指纹模拟，
  配一个全局令牌桶（`--rate`，默认 4 req/s），并对 403/408/429/5xx 重试退避。
* 实际用到的端点：`/public/collection/v1/objects/{id}`（JSON）和
  `https://www.metmuseum.org/art/collection/search/{id}`（HTML，只取 `og:image`）。

批量抓取刻意做得慢；只有能接受被限流时再调大 `--rate`/`--workers`。
馆方要求不超过 80 req/s；图片本身是 CC0 / 按件授权——转载非公有领域的作品前，
请先看 manifest 里的 `rights`（Rights and Reproduction）字段。

## 目录结构

```
painter/
├── cli.py            # 命令行入口：`painter met ...`
├── http.py           # 指纹模拟、限速、重试的 HTTP 客户端
├── naming.py         # 作者_作品_年代 命名规则
├── runner.py         # 爬取主循环、manifest、防撞名
└── sources/
    ├── base.py       # Work / ImageRef / Resolution / Source 协议
    └── met.py        # 大都会艺术博物馆
```

新增一个博物馆：实现 `Source`（`slug`、`iter_works()`、`resolve()`），
在 `sources/__init__.py` 里注册，并在 `cli.py` 里加一个子命令即可。
主循环、命名和 manifest 都与具体数据源无关。

## 测试

```bash
uv run pytest
```
