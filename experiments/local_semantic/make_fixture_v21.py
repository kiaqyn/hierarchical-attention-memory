"""Final fresh V2.1 holdout; V2's already-viewed 60 questions are regression only."""
import hashlib
import json
from pathlib import Path

TEST = [
    ("哪次调整让应用不再停留在刚打开时那张页面？", "boot"),
    ("What dependency change got the application past its initial waiting view?", "boot"),
    ("中文字符只剩矩形占位符，之前怎样补齐文字？", "font"),
    ("How did we get the missing Chinese characters to render instead of square placeholders?", "font"),
    ("产品售价的临时副本现在允许保留多长时间？", "ttl"),
    ("How long is a stored product price kept before it must be refreshed?", "ttl"),
    ("从身份提供方回来就必须重登的问题，以前修过什么？", "cookie"),
    ("What cookie change prevented a fresh sign-in being needed after the identity-provider redirect?", "cookie"),
    ("以前用哪个字段识别重复付款，让订单重发不会再多扣钱？", "payment"),
    ("What identifies the same purchase so retries cannot trigger an extra charge?", "payment"),
    ("编辑器和终端加载的Python环境不同，过去怎么统一？", "venv"),
    ("What did we align when the editor and terminal used different Python environments?", "venv"),
    ("没有文字层的扫描页，之前先做什么再检索？", "pdf"),
    ("What preparation is required before searching scanned pages with no text layer?", "pdf"),
    ("项目演示里的识字任务，每月经费确定成多少元？", "budget"),
    ("How much money was allocated per month to the synthetic OCR task?", "budget"),
    ("十月测试请求中有多大比例需要再次提交？", "metric"),
    ("What percentage of the October test orders had their requests retried?", "metric"),
    ("触发自动清理之前，访问审计能查到几天的记录？", "logs"),
    ("How many days of access audit history remain before automatic deletion?", "logs"),
    ("购买Excel许可证一共花了多少钱？", None, "unanswerable"),
    ("How much did the Excel license purchase cost?", None, "unanswerable"),
    ("处理字体问题这件事的批准人是谁？", None, "unanswerable"),
    ("Who approved the missing-glyph fix?", None, "unanswerable"),
    ("实际下载包的64位十六进制哈希值是什么？", None, "unanswerable"),
    ("What is the actual 64-character hexadecimal hash value of the package?", None, "unanswerable"),
    ("2024年测试订单的实测重试率是多少？", None, "unanswerable"),
    ("What retry percentage was measured for test orders in 2024?", None, "unanswerable"),
    ("支付成功率的实测百分比是多少？", None, "unanswerable"),
    ("What percentage of payments actually succeeded?", None, "unanswerable"),
    ("这件事怎么搞来着？", None, "ambiguous"),
    ("What should we do about that again?", None, "ambiguous"),
    ("织女风帆支线的关卡配色结论是什么？", None, "restricted"),
    ("What was the archived lake-blue completion-color decision?", None, "restricted"),
    ("为什么海水结冰时盐分会变化？", None, "unrelated"),
    ("How do spiders make their silk?", None, "unrelated"),
    ("交响乐队里的双簧管是什么乐器？", None, "unrelated"),
    ("Why do lunar eclipses not occur every month?", None, "unrelated"),
    ("人体缺水为什么会口渴？", None, "unrelated"),
    ("How do plants move water from roots to leaves?", None, "unrelated"),
]


def freeze(source, target):
    target.mkdir(parents=True,exist_ok=True)
    manifest = {"kind":"developer-authored fresh V2.1 holdout; prior V2 test is known regression",
                "test_frozen_before_v21_scoring":True,"files":{}}
    for name in ("corpus","dev","test"):
        content = ((source/(name+".json")).read_bytes() if name!="test" else
                   json.dumps([{"id":f"v21-test-{i:03d}","query":c[0],"expected":c[1],
                                "kind":c[2] if len(c)>2 else "positive"} for i,c in enumerate(TEST)],
                              ensure_ascii=False,indent=2).encode("utf8"))
        path = target/(name+".json")
        if path.exists() and path.read_bytes()!=content:
            raise ValueError("Refuse to replace the frozen final holdout")
        path.write_bytes(content)
        manifest["files"][name] = {"sha256":hashlib.sha256(content).hexdigest(),"count":len(json.loads(content))}
    (target/"freeze-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    import sys
    freeze(Path(sys.argv[1]),Path(sys.argv[2]))
