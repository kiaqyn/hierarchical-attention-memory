"""Freeze developer-authored synthetic data BEFORE threshold calibration."""
import hashlib
import json
from pathlib import Path

DOCS = [
    ("boot", "加载停滞修复", "将 Renderer 依赖固定为 4.2，清理本地缓存后验证启动通过。"),
    ("font", "缺字处理", "界面汉字显示方框，是字体缺少字形；打包 Noto Sans CJK 字体并配置字体回退。"),
    ("csv", "电子表格导出", "Excel 打开 CSV 出现中文乱码，导出文件使用 UTF-8-SIG，带 BOM。"),
    ("ttl_old", "旧缓存决定", "Redis 缓存有效期为 60 秒。"),
    ("ttl", "当前缓存决定", "产品价格 Redis 缓存有效期已从 60 秒改为 300 秒，商品更新后主动失效。"),
    ("cookie", "登录回跳", "跨站登录回调丢失会话，Cookie 改为 SameSite=Lax，并在 HTTPS 下设置 Secure。"),
    ("payment", "支付重复请求", "付款接口使用订单号作为幂等键，同一订单的重试返回原结果，避免重复扣款。"),
    ("sqlite", "数据库忙", "SQLite 并发写入出现 database is locked，启用 WAL，设置 busy_timeout=5000 毫秒。"),
    ("upload", "大文件上传", "大文件按 8 MiB 分片，保存已完成分片编号，断线后断点续传。"),
    ("pdf", "扫描文档提取", "扫描 PDF 没有文本层，先 OCR 识别图片中的文字，再建立检索索引。"),
    ("time", "时间显示约定", "数据库保存 UTC 时间，界面根据用户时区转换显示，不保存本地时间字符串。"),
    ("git", "换行符噪声", "Git diff 整个文件都变了是 CRLF/LF 混用，使用 .gitattributes 统一仓库为 LF。"),
    ("download", "下载完整性", "下载依赖后核对 SHA-256 校验值，不一致就拒绝加载，保留失败文件供检查。"),
    ("gpu", "显卡运行错误", "GPU 加速失败来自 CUDA 与驱动不兼容；使用匹配的运行时，暂时可切换 CPU。"),
    ("retry", "网络超时重试", "上游请求超时最多重试两次，使用指数退避和随机抖动，不能无限循环。"),
    ("venv", "解释器路径", "命令行装了包但 IDE 找不到，是 Python 解释器不同；IDE 选择项目 .venv 的解释器。"),
    ("build", "前端构建环境", "前端统一 Node 20，按现有锁文件执行 npm ci，不在构建时更新依赖版本。"),
    ("url", "中文文件链接", "中文文件路径的下载链接 404，路径各部分做 URL 编码，保留路径分隔符。"),
    ("keyboard", "键盘可访问性", "弹窗打开后焦点移入弹窗，Tab 在弹窗内循环，关闭后焦点返回触发按钮。"),
    ("branch", "支线专用口令", "织女风帆演示的通关颜色是靛紫色，仅供支线演示。"),
    ("archived", "已归档备选", "织女风帆备选通关颜色是湖蓝色，已归档，不再使用。"),
]

DEV = [
    ("软件进不了首页，当时最后怎么修好的？", "boot"),
    ("字变成一个个小框框，以前怎么解决？", "font"),
    ("Excel 读出来的中文不对，用哪种编码？", "csv"),
    ("商品改价后怎么防止一直显示旧价格？", "ttl"),
    ("授权返回后又要重新登录，怎么改 Cookie？", "cookie"),
    ("付款按钮多点几次也不能扣两笔，用了什么？", "payment"),
    ("数据库说被锁住了，后来怎么设置？", "sqlite"),
    ("传到一半掉网，怎样接着传大文件？", "upload"),
    ("只有图片的 PDF 怎样搜里面的字？", "pdf"),
    ("跨国用户看到的时间怎么统一保存？", "time"),
    ("代码看着没改但 diff 满屏红绿，怎么处理？", "git"),
    ("下载的安装依赖怎样确认没有损坏？", "download"),
    ("显卡跑不起来时的兼容方案是什么？", "gpu"),
    ("网络请求等不到响应，最多再试几次？", "retry"),
    ("终端能 import 但编辑器不能，是哪里的设置？", "venv"),
    ("打包前 npm 用什么命令安装已有版本？", "build"),
    ("带汉字的地址下载找不到文件怎么办？", "url"),
    ("对话框怎样让只用键盘的人正常操作？", "keyboard"),
    ("火星的空气主要是什么成分？", None, "unrelated"),
    ("今晚推荐吃哪道菜？", None, "unrelated"),
    ("橙色夜莺每月养育预算是多少？", None, "unrelated"),
    ("Renderer 固定版本花了多少钱？", None, "unanswerable"),
    ("扫描 PDF 的识别准确率实测是多少？", None, "unanswerable"),
    ("Noto 字体的品牌配色十六进制是什么？", None, "unanswerable"),
    ("它又坏了，之前怎么办的？", None, "ambiguous"),
    ("之前那个画面一直原地打转的问题怎么修？", None, "ambiguous"),
    ("支线织女风帆的通关颜色是什么？", None, "restricted"),
    ("湖蓝色备选通关口令是什么？", None, "restricted"),
    ("backup singer audition", None, "unrelated"),
    ("我家汽车钥匙丢了怎么处理？", None, "unrelated"),
]

TEST = [
    ("为什么那个启动时始终在等待的程序后来能打开了？", "boot"),
    ("When the app never gets past its opening screen, what was our fix?", "boot"),
    ("页面里的汉字全成豆腐块，过去采用了什么办法？", "font"),
    ("How did we restore Chinese glyphs missing from the interface?", "font"),
    ("给财务的逗号分隔文件，在微软表格里文字乱掉了，之前怎么导出？", "csv"),
    ("What encoding did we choose for spreadsheets to display Chinese correctly?", "csv"),
    ("价格缓存如今存多久，改完商品怎么刷新？", "ttl"),
    ("What is the current price-cache lifetime and invalidation policy?", "ttl"),
    ("第三方授权成功回到站点却像没登录过，旧决定是什么？", "cookie"),
    ("Which cookie attributes fixed sessions lost after the external sign-in callback?", "cookie"),
    ("顾客网络不好重复提交订单，怎样确保只付一笔？", "payment"),
    ("What makes repeated checkout requests safe for the same order?", "payment"),
    ("多个任务同时写 SQLite 时提示忙，原来怎么配置的？", "sqlite"),
    ("What settings helped when simultaneous SQLite writers blocked each other?", "sqlite"),
    ("大附件上传中断后不想从零开始，已有方案是什么？", "upload"),
    ("How can a large transfer resume after the connection drops?", "upload"),
    ("扫描件里复制不出文字，如何让它变得可检索？", "pdf"),
    ("What did we do before indexing image-only document pages?", "pdf"),
    ("同一事件在不同时区怎么存和展示才不会乱？", "time"),
    ("How should one event time be persisted for users in different time zones?", "time"),
    ("提交里所有行都像被修改了，实际上只是回车不同，怎么消掉噪声？", "git"),
    ("What repository setting prevents line-ending-only changes?", "git"),
    ("怎么避免把途中被改坏的依赖包加载进来？", "download"),
    ("Which digest must a downloaded dependency match before it is accepted?", "download"),
    ("加速卡和软件版本对不上时，当时的降级路线是什么？", "gpu"),
    ("What was our fallback when the graphics runtime and driver did not agree?", "gpu"),
    ("上游没响应要怎么间隔再试，能试到什么时候？", "retry"),
    ("What bounded retry schedule did we choose for upstream timeouts?", "retry"),
    ("明明已经安装模块，IDE 却报缺包，当时改了哪条路径？", "venv"),
    ("The editor cannot see a module that the shell imports. What did we align?", "venv"),
    ("怎样让每次前端打包都用锁定的依赖，而不是临时升级？", "build"),
    ("Which Node version and install command make the build follow the existing lockfile?", "build"),
    ("文件明明存在，只要名字带汉字链接就失效，用什么处理地址？", "url"),
    ("What fixed missing-download errors caused by non-ASCII path characters?", "url"),
    ("不碰鼠标时，打开和关掉模态窗口，光标应该怎么走？", "keyboard"),
    ("Where should keyboard focus go while a modal is open and after it closes?", "keyboard"),
    ("鲸鱼为什么要到海面换气？", None, "unrelated"),
    ("怎么烤出外脆内软的面包？", None, "unrelated"),
    ("帮我找附近的牙科诊所", None, "unrelated"),
    ("量子纠缠能用来超光速通信吗？", None, "unrelated"),
    ("What was the purchase price of our CUDA graphics card?", None, "unanswerable"),
    ("支付接口上个月实际重复扣款率是多少？", None, "unanswerable"),
    ("前端构建环境的管理员密码是什么？", None, "unanswerable"),
    ("UTC 时间方案已经节省了多少服务器费用？", None, "unanswerable"),
    ("那个还是不行，之前怎么弄的？", None, "ambiguous"),
    ("有个页面一直转圈，原来怎么处理？", None, "ambiguous"),
    ("Give me the secret completion color for the side quest 织女风帆", None, "restricted"),
    ("已归档的湖蓝色备选决定具体是什么？", None, "restricted"),
    ("不要查支付或缓存的旧记录", None, "restricted"),
    ("do not retrieve database backup history", None, "restricted"),
    ("Python programming python's pet snake enclosure cost", None, "unrelated"),
    ("机场登机口换了，行李要去哪里领取？", None, "unrelated"),
]


def freeze(directory):
    directory.mkdir(parents=True, exist_ok=True)
    docs = [{"id": key, "title": title, "body": body, "project": "S1" if key=="branch" else "M1",
             "status": "ARCHIVED" if key=="archived" else "ACTIVE",
             "supersedes": ["ttl_old"] if key=="ttl" else []} for key,title,body in DOCS]
    output = {"corpus": docs}
    for name, cases in (("dev", DEV), ("test", TEST)):
        output[name] = [{"id": f"{name}-{i:03d}", "query": case[0], "expected": case[1],
                         "kind": case[2] if len(case)>2 else "positive"} for i,case in enumerate(cases)]
    manifest = {"kind":"developer-authored synthetic; not external blind evaluation",
                "frozen_before_calibration":True, "files":{}}
    for name,data in output.items():
        path = directory/(name+".json")
        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf8")
        if path.exists() and path.read_bytes()!=content:
            raise RuntimeError("Refuse to overwrite an already frozen fixture: "+str(path))
        path.write_bytes(content)
        manifest["files"][name] = {"sha256":hashlib.sha256(content).hexdigest(),"count":len(data)}
    (directory/"freeze-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    import sys
    freeze(Path(sys.argv[1]))
