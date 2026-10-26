# 促销雷达

每天自动扫描马来西亚的机票、酒店旅游、银行卡、家私促销，整理成一个中文网页给家人朋友看。

- **扫描**：GitHub Actions 每天早上 8 点（马来西亚时间）运行 `scanner/scan.py`
- **AI**：Gemini 免费层（Google 搜索 + 内容整理）
- **网页**：GitHub Pages 托管 `docs/index.html`，读取 `docs/deals.json`

## 设置步骤（约 10 分钟）

1. **拿 Gemini API key**：到 https://aistudio.google.com 登录 → Get API key → 创建。
2. **建 repo**：在 GitHub 新建一个 **public** repo（例如 `promo-radar`），把这个文件夹的全部内容推上去。
3. **加 Secret**：repo → Settings → Secrets and variables → Actions → New repository secret，名称 `GEMINI_API_KEY`，值填第 1 步的 key。
4. **开 Pages**：Settings → Pages → Source 选 *Deploy from a branch*，Branch 选 `main`，文件夹选 `/docs`。
5. **第一次手动跑**：Actions → 每日扫描促销 → Run workflow。跑完后打开 `https://<你的用户名>.github.io/promo-radar/`。

## 本地测试

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=你的key
python scanner/scan.py --dry-run   # 只看结果，不写文件
```

## 调整扫描内容

全部在 `config.yaml`：
- `search_queries`：加/删搜索题目（每条每天消耗一次搜索额度）
- `fixed_pages`：加你常看的促销页网址
- `exclude_keywords`：要排除的航空公司或中转城市
- `model`：Gemini 模型名称（找不到模型时改这里）

## 注意

- 免费层的输入内容可能被 Google 用来改进模型，这里只送公开的促销搜索，没有个人资料。
- AI 整理的内容可能有错，下单前以商家页面为准。标着"链接未验证"的，表示原始链接打不开，换成了搜索来源。
- 扫描的是促销公告，不是实时票价。
