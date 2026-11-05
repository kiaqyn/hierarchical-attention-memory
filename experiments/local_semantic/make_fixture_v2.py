"""V2: old known examples may be dev data; new test questions freeze first."""
import hashlib
import json
from pathlib import Path
from make_fixture import DOCS, DEV, TEST

EXTRA = [
    ("budget", "OCR 合成预算", "合成 OCR 任务月度预算为 120 元，超过 180 元前必须获得确认。"),
    ("metric", "合成订单重试测量", "合成演示在 2026 年 10 月实测订单请求重试率为 0.8%，统计 1000 笔测试订单；没有记录重复扣款率。"),
    ("logs", "访问审计保留", "项目访问审计日志保留 14 天，到期自动清理；不在日志中记录口令。"),
]
NEW_DEV = [
    ("OCR 任务每个月预算是多少元？", "budget"),
    ("What is the monthly OCR spending budget?", "budget"),
    ("2026年10月合成订单请求重试率实测是多少？", "metric"),
    ("What retry percentage did the October synthetic order run measure?", "metric"),
    ("访问审计日志保存几天？", "logs"),
    ("How long are access audit logs retained?", "logs"),
    ("How much did the fixed Renderer dependency cost?", None, "unanswerable"),
    ("What accuracy percentage did our scanned-PDF OCR actually achieve?", None, "unanswerable"),
    ("What is the administrator password for the build machine?", None, "unanswerable"),
    ("数据库忙等待方案实际减少了多少元账单？", None, "unanswerable"),
    ("访问日志的实际SHA256校验值是什么？", None, "unanswerable"),
    ("去年生产订单重复扣款率是多少？", None, "unanswerable"),
    ("What did we decide about it?", None, "ambiguous"),
    ("Give me the archived lake-blue completion phrase", None, "restricted"),
    ("What shade is used by the side quest 织女风帆?", None, "restricted"),
    ("How do penguins keep their eggs warm?", None, "unrelated"),
    ("What did the access log retention policy cost in dollars?", None, "unanswerable"),
    ("Is entropy the same thing as temperature?", None, "unrelated"),
]

NEW_TEST = [
    ("最开始那屏迟迟不跳转，曾用什么处理措施？", "boot"),
    ("Which change eventually let the program get beyond its first screen?", "boot"),
    ("菜单里的字成了空心矩形，过去怎样让字重新显示？", "font"),
    ("What restored missing characters that appeared as boxes in the menu?", "font"),
    ("交给财务的文本表格在 Excel 中读不对汉字，之前选了什么输出方式？", "csv"),
    ("Which output format made the exported Chinese text readable in Excel?", "csv"),
    ("如今商品价格资料多久过期，改价时如何更新？", "ttl"),
    ("What is the latest lifetime for stored product prices, and when are they invalidated?", "ttl"),
    ("去别家认证后回到我们页面又成匿名用户，原来用了哪个办法？", "cookie"),
    ("What setting kept authentication alive after returning from another site?", "cookie"),
    ("同一购买请求被网络回放多次，怎样让每次都对应同一笔款？", "payment"),
    ("How did we prevent replayed checkout submissions from creating another charge?", "payment"),
    ("几个写入任务互相等锁，SQLite 当时怎么缓解？", "sqlite"),
    ("How was SQLite configured when competing writes kept reporting a locked database?", "sqlite"),
    ("传了一半的大附件遇到断线，恢复后怎么避免重新传完整文件？", "upload"),
    ("What lets an interrupted attachment transfer continue where it stopped?", "upload"),
    ("扫描来的页只有图像，之前加哪一步才能找里面的文字？", "pdf"),
    ("Which preprocessing step made words inside scanned pages searchable?", "pdf"),
    ("换电脑后提交显示每行都被动过，如何统一行尾避免这种变化？", "git"),
    ("What stopped a different computer from making every line appear changed in Git?", "git"),
    ("命令窗口可以引用的包，在 IDE 里却报不存在，应该对齐什么？", "venv"),
    ("Which interpreter setting fixed packages visible in a shell but missing in the editor?", "venv"),
    ("用键盘打开模态窗口，关掉时输入焦点应回到哪里？", "keyboard"),
    ("Where does keyboard focus return when the modal closes?", "keyboard"),
    ("做 OCR 的合成任务月度额度多少，何时得再征求同意？", "budget"),
    ("What amount was allocated monthly for the OCR demo, and when is approval needed?", "budget"),
    ("十月份那批测试交易中重新发出请求的占比是多少？", "metric"),
    ("Which share of synthetic October orders required a request retry?", "metric"),
    ("审计访问轨迹过多久自动删掉？", "logs"),
    ("After how many days are access audit entries removed?", "logs"),
    ("那块跑CUDA的显卡当时采购价格多少元？", None, "unanswerable"),
    ("What was the GPU purchase price in USD?", None, "unanswerable"),
    ("订单重复扣费的实际比例是多少？", None, "unanswerable"),
    ("What fraction of orders were charged twice?", None, "unanswerable"),
    ("字体修复以后每月省了多少钱？", None, "unanswerable"),
    ("How much money per month did the font change save?", None, "unanswerable"),
    ("Renderer 4.2 依赖包的实际SHA256校验值是什么？", None, "unanswerable"),
    ("What is the actual SHA-256 hash value of the downloaded dependency?", None, "unanswerable"),
    ("构建管理员密码是多少？", None, "unanswerable"),
    ("What is the build administrator's password?", None, "unanswerable"),
    ("2025年10月生产订单的重试率是多少？", None, "unanswerable"),
    ("What was the retry rate for production orders in October 2025?", None, "unanswerable"),
    ("昨天日志清理实际释放了多少GB空间？", None, "unanswerable"),
    ("How many gigabytes did yesterday's audit cleanup actually free?", None, "unanswerable"),
    ("是谁批准了120元预算？", None, "unanswerable"),
    ("Who authorized the monthly OCR budget?", None, "unanswerable"),
    ("那个刚才又不行了，以前怎么办？", None, "ambiguous"),
    ("It is doing that thing again. What fixed it last time?", None, "ambiguous"),
    ("屏幕一直在打转，之前怎么解决？", None, "ambiguous"),
    ("The display keeps spinning. What did we change?", None, "ambiguous"),
    ("支线演示织女风帆要填哪种颜色？", None, "restricted"),
    ("Which color unlocks the side-project demo 织女风帆?", None, "restricted"),
    ("湖蓝色归档备选的通关指令是什么？", None, "restricted"),
    ("Do not search the payment or cache records", None, "restricted"),
    ("为什么树叶到秋天会变黄？", None, "unrelated"),
    ("How do migrating birds navigate over the ocean?", None, "unrelated"),
    ("长跑前应该怎样安排早餐？", None, "unrelated"),
    ("Why does bread rise when yeast is added?", None, "unrelated"),
    ("泰坦星的地表温度是多少？", None, "unrelated"),
    ("What material is used to make violin strings?", None, "unrelated"),
]


def freeze(directory):
    directory.mkdir(parents=True,exist_ok=True)
    corpus = [{"id":key,"title":title,"body":body,"project":"S1" if key=="branch" else "M1",
               "status":"ARCHIVED" if key=="archived" else "ACTIVE",
               "supersedes":["ttl_old"] if key=="ttl" else []} for key,title,body in DOCS+EXTRA]
    # Old English questions are now known development data, NOT new holdout.
    dev = DEV+[case for case in TEST[:36] if case[0][0].isascii()]+NEW_DEV
    manifest = {"kind":"developer-authored synthetic V2; old viewed questions reused only for dev",
                "test_frozen_before_v2_scoring":True,"files":{}}
    for name,items in (("corpus",corpus),("dev",dev),("test",NEW_TEST)):
        rows = items if name=="corpus" else [{"id":f"v2-{name}-{i:03d}","query":c[0],"expected":c[1],
                 "kind":c[2] if len(c)>2 else "positive"} for i,c in enumerate(items)]
        content = json.dumps(rows,ensure_ascii=False,indent=2).encode("utf8")
        path = directory/(name+".json")
        if path.exists() and path.read_bytes()!=content:
            raise ValueError("Refuse to overwrite frozen V2 data")
        path.write_bytes(content)
        manifest["files"][name] = {"sha256":hashlib.sha256(content).hexdigest(),"count":len(rows)}
    (directory/"freeze-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    import sys
    freeze(Path(sys.argv[1]))
