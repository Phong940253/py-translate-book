# -*- coding: utf-8 -*-
"""One-time migration of the translated EPUB (chapters 130..450).

Fixes, in order, per chapter file:
  1. paragraph-level rewrites (Chinese paragraphs / translator notes / wrong names)
  2. chapter 410 full retranslation (was written in transliterated quốc-ngữ)
  3. chapter 411 per-paragraph `Trịnh Tông Hạo` disambiguation
  4. `p&gt;` structural corruption
  5. ordered name/fragment replacement table (longest first)
  6. chapter heading (`<h2>`) + `<title>` normalisation to `Chương N: <viet title>`

Usage:  python migrate_epub.py            # dry run, prints a report
        python migrate_epub.py --apply    # writes a backup then the EPUB
"""
import ast
import re
import shutil
import sys
import time
import unicodedata
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"F:\learning\py-translate-book"
EPUB = ROOT + r"\重生：开局逮到高冷校花超市偷窃.epub.translated.epub"
RULES_SRC = r"C:\Users\phong\AppData\Local\Temp\opencode\migrate_rules.py"
LO, HI = 130, 450

APPLY = "--apply" in sys.argv

CJKC = re.compile(r"[一-鿿]")
LATC = re.compile(r"[0-9A-Za-zÀ-ỹ]")

# --------------------------------------------------------------------------
# 1. load the authoritative pinyin/Chinese -> Vietnamese name maps
# --------------------------------------------------------------------------


class _Drop(ast.NodeTransformer):
    def visit_Name(self, node):  # noqa: N802
        if node.id == "DROP":
            return ast.copy_location(ast.Constant(value="__DROP__"), node)
        return node


def load_maps(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        name = None
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in ("KEY_MAP", "EXTRA"):
                name = t.id
        if name:
            out[name] = ast.literal_eval(_Drop().visit(node.value))
    return out


_MAPS = load_maps(RULES_SRC)
KEY_MAP, EXTRA = _MAPS["KEY_MAP"], _MAPS["EXTRA"]

# entries that must never touch the Vietnamese body text
EXTRA_BLOCK = {
    "你", "您", "我", "等", "们", "长", "师妹2", "学生们们的",
    "学生们的陆远秋、白清夏等", "学生的陆远秋、白清夏等",
    "陆远秋、白清夏、钟锦程等",
}

REPL = {}
for k, v in KEY_MAP.items():
    if v != "__DROP__":
        REPL[k] = v
for k, v in EXTRA.items():
    if not k.endswith("2") and k not in EXTRA_BLOCK:
        REPL[k] = v

# --------------------------------------------------------------------------
# 2. EPUB-specific corrections (CJK-in-name fragments + pinyin variants)
# --------------------------------------------------------------------------
EPUB_FIX = {
    # ---- Chung Cẩm Trình ----
    "Trình Cẩm Tr 程锦程": "Chung Cẩm Trình",
    "Chủng Cẩm": "Chung Cẩm",
    "Trợng Cẩm": "Chung Cẩm",
    "Trọng Cẩm": "Chung Cẩm",
    "Chu Cẩm": "Chung Cẩm",
    "Trung Cẩm": "Chung Cẩm",
    "CHung Cẩm": "Chung Cẩm",
    "Cẩm Tr 程": "Cẩm Trình",
    "Cẩm 程": "Cẩm Trình",
    "Cẩm Tr程": "Cẩm Trình",
    "Cẩm程": "Cẩm Trình",
    "程正在": "Trình đang",
    "程锦程": "Chung Cẩm Trình",
    "Trung Cẩm Trình": "Chung Cẩm Trình",
    "CHung Cẩm Trình": "Chung Cẩm Trình",
    # ---- Lục Viễn Thu ----
    "Lục Viên 秋": "Lục Viễn Thu",
    "Viễn秋": "Viễn Thu",
    "Lu远秋": "Lục Viễn Thu",
    "Lục远秋": "Lục Viễn Thu",
    # ---- Liễu Vọng Xuân ----
    "Liu Wang 春": "Liễu Vọng Xuân",
    "Liu Wang春": "Liễu Vọng Xuân",
    "Liu望春": "Liễu Vọng Xuân",
    "Wang春": "Vọng Xuân",
    # ---- Lục Dĩ Đông ----
    "Lu Yi冬": "Lục Dĩ Đông",
    "Lục 以冬": "Lục Dĩ Đông",
    "Lục以冬": "Lục Dĩ Đông",
    # ---- Bạch Tụng Triết ----
    "Bai Song哲": "Bạch Tụng Triết",
    "Bạch Song哲": "Bạch Tụng Triết",
    "Bai颂哲": "Bạch Tụng Triết",
    "Bạch颂哲": "Bạch Tụng Triết",
    "Bạch Sức哲": "Bạch Tụng Triết",
    "Bạch Tụng哲": "Bạch Tụng Triết",
    "Song哲": "Tụng Triết",
    # ---- other CJK name fragments ----
    "Song萱萱": "Tống Huyên Huyên",
    "Tr鄭一峰": "Trịnh Nhất Phong",
    "Tr郑一峰": "Trịnh Nhất Phong",
    "郑一峰摇头": "Trịnh Nhất Phong lắc đầu",
    "鄭一峰": "Trịnh Nhất Phong",
    "郑一峰": "Trịnh Nhất Phong",
    "Su妙妙": "Tô Diệu Diệu",
    "Tô Mi妙妙": "Tô Diệu Diệu",
    "苏妙妙": "Tô Diệu Diệu",
    "R阮": "Nguyễn",
    "阮月如": "Nguyễn Nguyệt Như",
    "和阮月如": "và Nguyễn Nguyệt Như",
    "苏老师": "Cô Tô",
    "Su老师": "Cô Tô",
    "吴妈": "Thím Ngô",
    "吳妈": "Thím Ngô",
    "徐阿姨": "Cô Từ",
    "梁先生": "Ông Lương",
    "道袍": "đạo bào",
    "đạo长": "đạo trưởng",
    "Lục Đậu晴": "Lục Đậu Tình",
    "Lu窦晴": "Lục Đậu Tình",
    "Đậu晴": "Đậu Tình",
    "Long怜冬": "Long Lân Đông",
    "Lương靖风": "Lương Tĩnh Phong",
    "Vệ 之玉": "Vệ Chi Ngọc",
    "Vệ Chi玉": "Vệ Chi Ngọc",
    "Chi玉": "Chi Ngọc",
    "Luo薇": "La Vi",
    "Tinh卫": "Tinh Vệ",
    "Thiếu爷": "Thiếu gia",
    "thiếu爷": "thiếu gia",
    "Châu V湾 One": "Châu Loan One",
    "Châu V湾 Nhất Hào": "Châu Loan One",
    "Cao强": "Cao Cường",
    "Lục渊": "Lục Uyên",
    "Đại叔 Trương Văn Quân": "Chú Trương Văn Quân",
    "Đại叔": "Chú",
    "đại叔": "chú",
    # ---- word-level CJK leftovers ----
    "chào奶奶 tạm biệt": "chào tạm biệt bà nội",
    '"phú婆 Hạ"': '"cô Hạ giàu"',
    "tự言tự ngữ": "tự lẩm bẩm",
    "rồi好吧": "rồi, được chưa",
    "bị张逸权": "bị Trương Dật Quyền",
    "bị冻": "bị lạnh",
    "thoát困": "thoát khốn",
    "vô聊": "vô bổ",
    "muốn吐槽": "muốn chê",
    "tự责": "tự trách",
    "nhỏ声": "nhỏ giọng",
    "quốc庆": "Quốc Khánh",
    "thuần洁": "thuần khiết",
    "xuất竅": "xuất khiếu",
    "chọn母校": "chọn ngôi trường cũ",
    "biến通": "biến thông",
    "bầu空气": "bầu không khí",
    "cảm觉得": "cảm thấy",
    "giao集": "giao tiếp",
    "hành廊": "hành lang",
    "ngũ指": "năm ngón tay",
    "cường壮": "cường tráng",
    "cười嘻嘻": "cười hì hì",
    "khung画面": "khung cảnh",
    "trên贴吧": "trên diễn đàn",
    "của导员": "của cố vấn",
    "trò恶趣味": "trò ác ý",
    "trò惡趣味": "trò ác ý",
    "đến蹭": "đến chen vào",
    # ---- leftovers found by the dry run ----
    "遠秋 ca ca": "anh Viễn Thu",
    "遠秋": "Viễn Thu",
    "逸彬": "Dật Bân",
    "欣赏 (tán thưởng)": "tán thưởng",
    "欣赏": "tán thưởng",
    "感動": "cảm động",
    "cảm動": "cảm động",
    "動": "động",
    "Su Tiểu雅": "Tô Tiểu Nhã",
    "Tiểu雅": "Tiểu Nhã",
    "一叶知秋": "Nhất Diệp Tri Thu",
    "H哈哈哈": "Ha ha ha",
    "哈哈哈": "ha ha ha",
    "哈哈": "ha ha",
    "哈": "ha",
    "đạo袍": "đạo bào",
    "袍": "bào",
    "盯着 (nhìn chằm chằm)": "nhìn chằm chằm",
    "盯着": "nhìn chằm chằm",
    "Trình正在": "Trình đang",
    "Chi Cao Cao": "Trì Thảo Thảo",
    "Trì Cao Cao": "Trì Thảo Thảo",
    "Chi Cao 草": "Trì Thảo Thảo",
    "草": "Thảo",
    "呀": "",
    # ---- Vietnamese-surname + pinyin-given-name hybrids ----
    "QingXia": "Thanh Hạ",
    "Qing Xia": "Thanh Hạ",
    "Bai QingXia": "Bạch Thanh Hạ",
    "Bạch Qing Xia": "Bạch Thanh Hạ",
    "Yuan Qiu": "Viễn Thu",
    "Lục Yuan Qiu": "Lục Viễn Thu",
    "Vẫn Qiu": "Viễn Thu",
    "Lục Vẫn Qiu": "Lục Viễn Thu",
    "Yi Feng": "Nhất Phong",
    "Trịnh Yi Feng": "Trịnh Nhất Phong",
    "Jin Cheng": "Cẩm Trình",
    "Chung Jin Cheng": "Chung Cẩm Trình",
    "Wang Chun": "Vọng Xuân",
    "Liuzhang Wang Chun": "Liễu Vọng Xuân",
    "Jing Feng": "Tĩnh Phong",
    "Lương Jing Feng": "Lương Tĩnh Phong",
    "Miaomiao": "Diệu Diệu",
    "Su Miaomiao": "Tô Diệu Diệu",
    "Tô妙妙 (Su Miaomiao)": "Tô Diệu Diệu",
    "Liễu Kiến Qing": "Liễu Kiến Thanh",
    "Xia Xia": "Hạ Hạ",
    "Xiao Xiao Chen": "Tiểu Tiểu Trần",
    "Gan Sha": "Cảm Sa",
    "không缓": "chưa hoàn hồn",
    "xe赶来": "xe tới nơi",
    "hội合": "hội họp",
    "khí场": "khí thế",
    "của连": "của đại đội",
    "học姐": "học tỷ",
    "Thu哥": "anh Thu",
    "nam模先生": "anh người mẫu",
    "học园区": "khuôn viên",
    "学习园区": "khuôn viên",
    "Sinh X鲜": "Sinh Tươi",
    "X鲜": "Tươi",
    "Tô妙妙": "Tô Diệu Diệu",
    "Mi妙妙": "Diệu Diệu",
    "ngài倒是": "ngài",
    "Tao倒是": "Tao thì",
    "Tiểu萝莉": "Bé loli",
    "tiểu萝莉": "bé loli",
    "đại萝莉": "chị loli",
    "Đại萝莉": "Chị loli",
    "xào准备": "xào chuẩn bị",
    "xào準備": "xào chuẩn bị",
    "准备": "chuẩn bị",
    "準備": "chuẩn bị",
    "半信半疑": "nửa tin nửa ngờ",
    "語重心長 nói:": "nói với giọng chân thành:",
    "語重心長": "nói với giọng chân thành",
    "语重心长": "nói với giọng chân thành",
    "khẩu hình miệngên cuồng": "khẩu hình điên cuồng",
    "奶奶": "bà nội",
    # ---- Latin-only name fixes ----
    "Trịnh Y Phong": "Trịnh Nhất Phong",
    "Lãnh Viễn Thu": "Lục Viễn Thu",
    "Lục Hành Châu": "Lục Hành Chu",
    "Tiga Ultraman": "Ultraman Tiga",
    "Hạ Nhất Vản": "Hạ Nhất Uyển",
    "Hạ Nhất Oản": "Hạ Nhất Uyển",
    "Mì Hạ Nhất Vản": "Mì Hạ Nhất Uyển",
    "Lý Luo Zhen": "Lý La Trân",
    "Luo Zhen": "Lý La Trân",
    "Bao Ya Zhen": "Lý La Trân",
    "Ca Sảng": "anh Sảng",
    "Cao Shuang": "Tào Sảng",
    "Lệ tiên sinh": "Ông Lương",
    "Liucheng Ye": "Liễu Thừa Nghiệp",
    "Lục Dĩ Thu": "Lục Dĩ Đông",
    "Lục Viễn Thuận": "Lục Dĩ Đông",
    "Cao Khang": "Cao Cường",
    "Lư Thành": "Lô Thành",
    "Ga Nam Lư Thành": "Ga Nam Lô Thành",
    "Anh Cầu": "anh Thu",
    "Đại Cầu": "anh Thu",
    "Viên Cầu": "Viễn Thu",
    "Tiểu Cầu": "Tiểu Thu",
    "Anh Qiu": "anh Thu",
    "Ca Thu": "anh Thu",
    "Qiu Qiu": "Thu Thu",
    "Ban Dao Yuan": "Hứa Văn Cường",
    "Ouyang Teacher": "Thầy Âu Dương",
    "Zhang Aunt": "Cô Trương",
    "San Shen Shen": "Thím Ba",
    "Chu Wen Xuan": "Sở Văn Huyên",
    "Tan Le": "Đàm Lạc",
    "Chị Li": "Chị Lệ",
    "Zhang Li": "Chị Lệ",
    "Zhang": "Trương",
    "Long Lian": "Long Lân Đông",
    "Chung Cường": "Chung Cẩm Trình",
    "Trương Thượng Phong": "Trương Tam Phong",
    "Lương Cường": "La Cường",
    "Đại thiếu gia": "Đại thiếu gia",

    # ---- canonical name variants (pinyin / mistransliterations) -------------
    "Lục Nguyên Thu": "Lục Viễn Thu",
    "Lục Viên Thu": "Lục Viễn Thu",
    "Lục Vườn Qiu": "Lục Viễn Thu",
    "Lục Vườn": "Lục Viễn",
    "Lục Vễn Thu": "Lục Viễn Thu",
    "Lục Hưêu Thu": "Lục Viễn Thu",
    "Lục Sân Viễn": "Lục Viễn Thu",
    "Lục Far Đông": "Lục Viễn Thu",
    "Lục Tả Vĩ": "Lục Viễn Thu",
    "Lục Tiễn": "Lục Thiên",
    "Lục Tiếng": "Lục Thiên",
    "Lục Tian": "Lục Thiên",
    "Lục Tiên": "Lục Thiên",
    "Lục Tiêu": "Lục Thiên",
    "Chung Cảnh Trình": "Chung Cẩm Trình",
    "Chung Cảnh Thành": "Chung Cẩm Trình",
    "Chung Cảnh": "Chung Cẩm",
    "Trịnh Một Phong": "Trịnh Nhất Phong",
    "Trịnh Di Phong": "Trịnh Nhất Phong",
    "Trịnh Dĩ Phong": "Trịnh Nhất Phong",
    "Trịnh Dật Phong": "Trịnh Nhất Phong",
    "Trịnh Hữu Phong": "Trịnh Nhất Phong",
    "Trịnh Hưng Phong": "Trịnh Nhất Phong",   # ch512/600 wrote 兴-style reading
    "Trịnh Tường": "Trịnh Nhất Phong",
    "Trương Wen Jun": "Trương Văn Quân",
    "Trương Li": "Trương Lệ",
    "Trương Lợi": "Trương Lệ",
    "Chị Lợi": "Chị Lệ",
    "chị Lợi": "chị Lệ",
    "chị Li": "chị Lệ",
    "Thầy Xu": "Thầy Từ",
    "thầy Xu": "thầy Từ",
    "Xu Siyang": "Hứa Si Yang",
    "Bạch Triết Triết": "Bạch Tụng Triết",
    "Fengel": "Tống Phân Phương",
    "Fenger": "Tống Phân Phương",
    "Fengeer": "Tống Phân Phương",
    "Phingel": "Tống Phân Phương",
    "Phinge": "Tống Phân Phương",
    "Phing Ge Er": "Tống Phân Phương",
    "Tống Fen Fang": "Tống Phân Phương",
    "Cao Sảng": "Tào Sảng",
    "Ca Sảng": "anh Sảng",
    "Ca Thu": "anh Thu",
    "Long Liandong": "Long Lân Đông",
    "Long Liên Đông": "Long Lân Đông",
    "Long Lei Đông": "Long Lân Đông",
    "Ran Ran": "Nhiên Nhiên",
    "Xuan Xuan": "Huyên Huyên",
    "Liang": "Lương",
    "Uông Tô Lang": "Uông Tô Lung",
    "Yue": "Ọe",
    # song lyrics left as han-nom / pinyin with a bracketed gloss (ch338)
    "“Nan dĩ vượng ký, sơ thứ kiến nhĩ.” (Khó lòng quên được, lần đầu gặp em.)":
        "“Khó lòng quên được, lần đầu gặp em.”",
    "“Nhất song mê nhân đích nhãn tinh.” (Một đôi mắt mê hoặc.)":
        "“Một đôi mắt mê hoặc.”",
    "“Tại ngã đích não hải lý.” (Ở trong tâm trí anh.)":
        "“Ở trong tâm trí anh.”",
    "“Nhĩ đích thân ảnh.” (Bóng hình em.)":
        "“Bóng hình em.”",
    "“Hôi tán bất khứ~” (Vẫn mãi không phai mờ~)":
        "“Vẫn mãi không phai mờ~”",
    "“Chỉ phạ tự kỷ hội ái thượng nhĩ, dã hứa hữu thiên hội tình bất tự kỉnh.” "
    "(Chỉ sợ bản thân sẽ yêu em, có lẽ ngày nào đó sẽ không kiềm được lòng.)":
        "“Chỉ sợ bản thân sẽ yêu em, có lẽ ngày nào đó sẽ không kiềm được lòng.”",
    "“Tưởng niệm chỉ nhượng tự kỷ khổ liễu tự kỷ, ái thượng nhĩ thị ngã tình phi đắc dĩ.” "
    "(Nỗi nhớ chỉ khiến tự mình làm khổ mình, yêu em là nỗi lòng chẳng thể khác đi.)":
        "“Nỗi nhớ chỉ khiến tự mình làm khổ mình, yêu em là nỗi lòng chẳng thể khác đi.”",
    "“Phá phủ trầm châu chiến cao tam, bối thủy nhất chiến sáng kỳ tích, thanh xuân vô hối, "
    "cao khảo tất thắng!”":
        "“Đập nồi đắm thuyền quyết thắng năm cuối, lưng tựa sông lập thành tích tốt, "
        "tuổi trẻ không hối tiếc, thi đại học chắc thắng!”",
    "Zheng Jing Cheng": "Chung Cẩm Trình",
    "Lu Xingzhou": "Lục Hành Chu",
    "Liu Wangchun": "Liễu Vọng Xuân",
    "Liu Jianqing": "Liễu Kiến Thanh",
    "Liễu Kiến Qing": "Liễu Kiến Thanh",
    "Tiểu Xia": "Tiểu Hạ",
    "Mì Một Bát Của Xia": "Mì Hạ Nhất Uyển",
    "Hạ Nhất Uyển Miến": "Mì Hạ Nhất Uyển",
    "Mì Hạ Nhất ra": "Mì Hạ Nhất Uyển ra",
    "họ Lục Lục Hành": "họ Lục, Lục Hành",
    # ---- camelCase / glued pinyin leftovers ----
    "Lục YuanQiu": "Lục Viễn Thu",
    "YuanQiu": "Viễn Thu",
    "Lục Yuan Qiu": "Lục Viễn Thu",
    "Zhong Jing Chengếc": "Chung Cẩm Trình liếc",
    "Long Ling Dong": "Long Lân Đông",
    "XuXu": "Xuân Xuân",
    "Chun Chun": "Xuân Xuân",
    "Yi Ye Zhi Thu": "Nhất Diệp Tri Thu",
    "anh Ruo An": "anh Nhược An",
    "Ruo An": "Nhược An",
    "A Wei": "A Vi",
    "Cao Qiang": "Cao Cường",
    "dì Luo": "dì Lạc",
    "Hạnh Phúc Li": "Hạnh Phúc Lý",
    # ---- cross-chapter name variants (every entry checked against the CN source) ----
    # residue glued onto a correct name
    "Lục Viễn Thuđđđđđđđược": "Lục Viễn Thu được",
    "Bạch Thanh Hạáp": "Bạch Thanh Hạ áp",
    "ཁPhía": "Phía",
    "Lục Viễn Thuếc": "Lục Viễn Thu liếc",
    # 陆远秋
    "Lục Viễn Thuỷ": "Lục Viễn Thu",
    "Lục Viễn Cầu": "Lục Viễn Thu",
    "Lưu Viễn Thu": "Lục Viễn Thu",
    # 钟锦程
    "Chung Cẩm Chương": "Chung Cẩm Trình",
    "Chung Cẩm Thành": "Chung Cẩm Trình",
    "Chung Cẩm Trầm": "Chung Cẩm Trình",
    "Chung Cẩm Trông": "Chung Cẩm Trình",
    "Chung Cẩm Trợn": "Chung Cẩm Trình",
    # 白颂哲
    "Bạch Sơn Triết": "Bạch Tụng Triết",
    "Bạch Tông Triết": "Bạch Tụng Triết",
    "Bạch Tấn Triết": "Bạch Tụng Triết",
    # 柳见青 / 柳望春 / 柳承业
    "Liễu Kiến Kiến": "Liễu Kiến Thanh",
    "Lưu Kiến Thanh": "Liễu Kiến Thanh",
    "Lưu Cương Nghiệp": "Liễu Thừa Nghiệp",
    "Liễu Hợp Nghiệp": "Liễu Thừa Nghiệp",
    "Lưu Vọng Xuân": "Liễu Vọng Xuân",
    # 梁靖风
    "Lương Trưng Phong": "Lương Tĩnh Phong",
    "Lượng Giấn Phong": "Lương Tĩnh Phong",
    "Lượng Tĩnh Phong": "Lương Tĩnh Phong",
    # 刘梓轩 / 刘薇
    "Liễu Tử Hiên": "Lưu Tử Hiên",
    "Lưu Tử Xuan": "Lưu Tử Hiên",
    "Lưu Vi Sương": "Lưu Vi",
    "Lưu Vi Thường": "Lưu Vi",
    "Lưu Vi Văn": "Lưu Vi",
    "Lưu Vi Vi": "Lưu Vi",
    "Lưu Viện": "Lưu Vi",
    # 宋萱萱 / 宋芬芳
    "Tống Tuyên Tuyên": "Tống Huyên Huyên",
    "Tùng Huyên Huyên": "Tống Huyên Huyên",
    "Tống Tiên Phương": "Tống Phân Phương",
    "Tùng Phân Phương": "Tống Phân Phương",
    # misc single-occurrence / minority spellings
    "Dương Cốc Huyên": "Dương Cốc Huyền",
    "Nhân Gian Tiên Hỏa": "Nhân Gian Yên Hỏa",
    "Hạnh Phúc Lợi": "Hạnh Phúc Lý",
    "Quế Hoa Hạng": "Quế Hoa Hẻm",
    "Vệ Diêm Ngọc": "Vệ Chi Ngọc",
    "Phù Lịch Mộng": "Phù Dịch Mộng",
    "Phù Dị Mộng": "Phù Dịch Mộng",
    "Hồ Thái Vi": "Hồ Thải Vi",
    "Trần Phỉ": "Trần Phi",
    "Trần Phí": "Trần Phi",
    "Lý Đặc Kiêu": "Lý Đặc Kiều",
    "Trương Dật Tân": "Trương Dật Bân",
    # 苏妙妙
    "Tô Miêu Miêu": "Tô Diệu Diệu",
    "Tô Miểu Miểu": "Tô Diệu Diệu",
    "Tô Miệu Miệu": "Tô Diệu Diệu",
    "Tô Mộ Mộ": "mẹ Tô",
    "Tô Mụ Mụ": "mẹ Tô",
    # 小秋 / 楚澜 / 高强 / 曹爽
    "Tiểu Khẩu": "Tiểu Thu",
    "Sở Lam": "Sở Lan",
    "Cao Khương": "Cao Cường",
    "Cao Thường": "Cao Cường",
    "Cao Sướng": "Cao Cường",
    "Tào Sương": "Tào Sảng",
    # ---- CJK punctuation / kana residue ----
    "rốtはや": "rốt cuộc",
    "゜": "°",
    "｀": "`",
    "「": "“",
    "」": "”",
    # ---- single-token pinyin surnames / nouns ----
    "Dong": "Đông",
    "Qin": "Tần",
    "Qiang": "Cường",
    "Li": "Lý",
    # single-token surnames / roles (word-boundary matched, see _ascii_word_pat)
    "Lu": "Lục",
    "Liu": "Lưu",
    "Su": "Tô",
    "Bai": "Bạch",
    "Qiu": "Thu",
    "Xia": "Hạ",
    "Zheng": "Trịnh",
    "Xu": "Từ",
    # ---- run-2 glue family (checkpoint chapters; CN source verified) --------
    # mirrors of rules that existed only in simplified/other variants:
    "錦程": "Cẩm Trình",                  # Chung錦程 -> Chung Cẩm Trình
    "Liễu望春": "Liễu Vọng Xuân",
    "La薇": "La Vi",
    "thím徐": "Cô Từ",
    "Thím徐": "Cô Từ",
    "Lân冬": "Lân Đông",                  # inside Long Lân冬
    "Lâm黛玉": "Lâm Đại Ngọc",
    "Trần呈": "Trần Trình",               # 呈 read as trình
    "Thanh璇": "Thanh Tuyền",             # Lý Thanh璇
    "Lục曹": "Lục Tào",                   # Lục曹 thị
    "Lệ姐": "Chị Lệ",
    "Nam主任": "Nam chủ nhiệm",           # CN男主任 — a role, not a name
    "连载": "đăng theo kỳ",
    "\u9023载": "đăng theo kỳ",           # 連载 (traditional) body variant
    # user-approved choices:
    "Bạch犀": "Bạch Kê",                  # CN白犀 noodle brand (ch650)
    "đại ngoại甥": "thằng cháu lớn",      # CN大外甥 (ch741)
    # remaining name glue:
    "Lý 博文": "Lý Bắc Văn",
    "Tiểu秋": "Tiểu Thu",
    "Tiểu Kh秋": "Tiểu Thu",
    "Lục窦Tình": "Lục Đậu Tình",
    "Dật彬": "Dật Bân",
    "Hoa盈": "Hoa Doanh",                 # 花楹 role in仙剑三
    "Sảng子": "Sảng tử",
    "A Tr程": "A Trình",
    "Tiểu Ph芬": "Tiểu Fin",              # CN小芬芬, pun on Finkel
    "Tiểu L梨": "Tiểu Lê",
    "Lương爹": "Bố Lương",
    "Đại di妈": "Đại dì",                 # CN大姨妈 joke (ch649)
    "Đông冬": "Đông Đông",                # CN冬冬 reduplication
    "Đông嘿嘿嘿": "Đông hehehe",
    "春春": "Xuân Xuân",                  # Liễu Vọng Xuân's nickname
    "钟宝": "Chung Bảo",                  # chat nicknames (ch491..494)
    "薇宝": "Vi Bảo",
    "L骆": "Lạc",                         # matches existing "Cô Lạc"
    "tiểu蘿lợi": "tiểu loli",             # CN小萝莉
    "Tào倩倩": "Tào Thiến Thiến",
    "Đinh婉君": "Đinh Uyển Quân",
    "Vĩnh\u8cb4": "Vĩnh Quý",             # Trần Vĩnh貴 -> Trần Vĩnh Quý
    "Đại Băng Ch钟": "Đại Băng Chung",    # canon already ships x12
    "程 và 薇": "Trình và Vi",
    "cười憨": "cười hồn",                 # CN憨厚 -> hồn hậu
    "hình如": "hình như",
    ",导员Cường": ", cố vấn Cường",       # house style: "của导员" -> "của cố vấn"
    # remaining word glue:
    "贴吧": "diễn đàn",
    "không拿出": "không đưa ra được",
    "题目": "đề bài",
    "nghĩ思路": "nghĩ cách giải",
    "付出": "cống hiến",
    "吐槽": "chê",                        # house style: "muốn吐槽" -> "muốn chê"
    "trương\u8abf": "cao điệu",           # trương調 (traditional調)
    "cao\u8c03": "cao điệu",              # cao调
    "ghế s沙发": "ghế sofa",
    "幸灾乐祸 (họa vô đơn chí/vui trên nỗi đau của người khác)":
        "vui trên nỗi đau của người khác",
    "thế外桃源": "thế ngoại đào nguyên",
    "phòng患于未然 (phòng bệnh hơn chữa bệnh)": "phòng bệnh hơn chữa bệnh",
    "đa tình tự作多情": "tự đa tình",
    "được交代": "được nêu rõ",
    "Cô全程 gật đầu": "Suốt buổi cô chỉ gật đầu",
    "chẳng lẽ一般 đều đại diện cho": "thường có nghĩa là",
    "cậu复学复.lại": "cậu quay lại",
    "Truyền T传媒": "Truyền Thông",
    "tự lập门户": "tự lập nghiệp",
    "ân o怨 hận": "ân oán thù hận",
    "đại c舅anh": "anh vợ",
    "cáo\u8fad": "cáo từ",                # cáo辭 (traditional辞)
    "tướng phu妻": "tướng vợ chồng",
    "đại\u8166": "bộ não",                # đại não (traditional腦)
    "quân训 (quân sự)": "quân sự",
    # leftovers found by run-2 dry pass:
    "Thanh S葱": "Thanh Thông",           # 青葱咖啡店 (CN ch458)
    "Lục thị集团": "tập đoàn Lục thị",     # 陆氏集团 (CN ch469)
    "phú婆": "phú bà",                    # 小富婆
    "劳资……蜀道山": "Tao đếm tới ba",      # 蜀道山 = 数到三 (CN ch552)
    "劳资蜀道山": "Tao đếm tới ba",
    "“呃……”": "“Ờ……”",
    "“妙” (Diệu)": "“Diệu”",              # cup label ch707
    "\"妙\"": "\"Diệu\"",                 # costume labels ch670
    "口碑": "danh tiếng",                  # CN ch665 口碑发酵
    "Nông夫山泉 (Nông Phu Sơn Tuyền)": "Nông Phu Sơn Tuyền",  # CN ch774
    # canon splits backed by ...translated.rules.json (canonical:count vs variant:count):
    "Lục Đấu Tình": "Lục Đậu Tình",       # 陆窦晴 — 314 vs 9
    "Lục Tào Thị": "Lục Tào thị",         # 陆曹氏 — 115 vs 0
    "Đại Thúc": "Đại thúc",               # canonical — 60 vs 0
    # brand夏一碗面 -> canon "Mì Hạ Nhất Uyển":
    "mỳ Tô Nhất V碗": "mì Hạ Nhất Uyển",
    "mì Nhất Uy碗": "mì Hạ Nhất Uyển",
    "Mì Hạ Nhất V碗": "Mì Hạ Nhất Uyển",
    "mì Hạ Nhất M碗": "mì Hạ Nhất Uyển",
    "Hạ Nhất V碗 面": "Mì Hạ Nhất Uyển",
    '"夏一碗面" (Mì một tô nhà họ Hạ)': '"Mì Hạ Nhất Uyển"',
}
REPL.update(EPUB_FIX)

ORDER = sorted(REPL.items(), key=lambda kv: (-len(kv[0]), kv[0]))


_GUARD = r"[A-Za-z0-9\u00c0-\u024f\u1e00-\u1eff]"


def _ascii_word_pat(pat):
    """Pure-ASCII word patterns are matched on ASCII-letter boundaries so that
    e.g. ``Su`` never hits the middle of ``Sushi`` / ``Suốt``, while still
    matching when the token sits directly against CJK text."""
    if not pat or not pat.isascii():
        return None
    if not (pat[0].isalnum() and pat[-1].isalnum()):
        return None
    if not all(ch.isalnum() or ch == " " for ch in pat):
        return None
    return re.compile(r"(?<!%s)%s(?!%s)" % (_GUARD, re.escape(pat), _GUARD))


_BWP = {}
_MISS = object()


def _pattern_rx(pat):
    rx = _BWP.get(pat, _MISS)
    if rx is _MISS:
        rx = _ascii_word_pat(pat) or re.compile(re.escape(pat))
        _BWP[pat] = rx
    return rx


def sub_one(text, pat, repl):
    def f(m):
        out = repl
        if CJKC.match(pat[0]) and m.start() > 0 and LATC.match(text[m.start() - 1]):
            out = " " + out
        if CJKC.match(pat[-1]) and m.end() < len(text) and LATC.match(text[m.end()]):
            out = out + " "
        return out
    return _pattern_rx(pat).sub(f, text)


# --------------------------------------------------------------------------
# 3. paragraph level rewrites  {file: {index: (expected_old, new)}}
# --------------------------------------------------------------------------
SPECIAL = {
    140: {56: ("曾许人间第一流",
               'Lại có kẻ dùng mực đen thêm một nét dọc và một nét phẩy bên dưới chữ "Nhất"'
               ' trong câu "Từng hứa nhân gian đệ nhất lưu", thế là chữ "Nhất" liền biến'
               ' thành chữ "Hạ".')},
    172: {0: ("节目单啊",
              "“Danh sách tiết mục à…… tôi không phụ trách khâu này trong trường, "
              "cũng không rõ bây giờ tiến độ tới đâu rồi.”")},
    175: {58: (r"利 \(lợi\) mã", "Lũ khốn kiếp! Lấy hời của tôi à!")},
    229: {14: (r"往昔不可追",
                "《Quá khứ không thể đuổi theo, nhưng vẫn lấp lánh trong trí nhớ — Đêm hội "
                "mừng năm mới trường Trung học Số 7 Lô Thành》")},
    260: {6: ("bỏ qua đoạn code-like",
              "“Sói xám……” Lục Viễn Thu khẽ cười nhạo, nhỏ giọng lẩm bẩm: "
              "“Tôi sớm đã không gây chuyện nữa rồi, được chưa.”")},
    282: {6: ("郑一峰摇头：“Vâng.”", "Trịnh Nhất Phong gật đầu: “Vâng.”")},
    298: {0: ("陆天：",
              "Lục Thiên: “Hay là ban ngày con cũng tới siêu thị làm, "
              "lương thì xem tình hình rồi tăng thêm cho con?”")},
    299: {60: ("Tr郑一峰",
               "Lục Viễn Thu hai tay bưng mâm vừa bước ra từ bếp sau, lớn tiếng đáp lại: "
               "“Ông đây mẹ...” Cậu khựng lại, liếc nhìn bé gái tầm năm sáu tuổi ở bàn ăn "
               "bên cạnh, vội vàng đổi lời: “Tôi đang bưng món đây! Tìm Trịnh Nhất Phong ấy! "
               "Trịnh Nhất Phong đâu rồi?!”")},
    317: {0: ("啪嗒", "“Cạch.”")},
    348: {31: ("旸谷",
               "Long Lân Đông với giọng điệu bình thản giải thích: \"'Dương Cốc' chính là "
               "nơi mặt trời mọc trong truyền thuyết đấy.\"")},
    363: {46: ("既然 (từ này sót",
               "Lúc này cô chợt nhắc nhở một câu: “Tuy nhiên... trên lầu ba cũng có học tỷ "
               "thuộc khoa Mỹ thuật, đã những học tỷ khoa Mỹ thuật ở lầu hai dính líu vào "
               "chuyện này, vậy các học tỷ ở trên lâu có khi nào cũng có hiềm nghi không?”")},
    403: {0: ("用力把你肌肉",
              "“Giương rõ đường nét cơ bắp của cậu ra nào…… Ơ? Chàng trai, tập luyện đúng là "
              "không tệ đấy, nhìn cơ bắp này xem, có bạn gái chưa?”")},
    412: {21: ("天之骄女|thiên之骄女",
               "Sự thay đổi của con người quả thực rất lớn, là thiên chi kiêu nữ từ nhỏ đã "
               "đứng trên đỉnh cao, không ngờ cũng có ngày phải hạ mình mặt dày đến chen "
               "vào hưởng sự náo nhiệt của người khác.")},
    # ch411 handled separately (NAME411, per paragraph)
}

# ch411: `Trịnh Tông Hạo` is ambiguous -> per paragraph target
NAME411 = {0: "Trịnh Nhất Phong", 38: "Trịnh Nhất Phong", 46: "Trịnh Nhất Phong",
           40: "Chung Cẩm Trình", 43: "Chung Cẩm Trình", 44: "Chung Cẩm Trình"}

# `Xia Xia` = 夏夏 -> Hạ Hạ, but 小夏 -> Tiểu Hạ
XIA_HA_FILES = {137, 169, 215}

# tokens that must not be touched by the replacement table (accent joke etc.)
PROTECT = [("Su Su", "\ue000")]

# --------------------------------------------------------------------------
# 4. chapter 410: full retranslation
# --------------------------------------------------------------------------
CH410 = [
    "Bên rìp khán giả, một cô bé mặc áo múa ballet trắng tinh cúi người thấp xuống, cúi "
    "đầu, chạy vội lên theo dọc bậc thang bên hông.",
    "Dù đã hạ đầu xuống thấp, nhưng các học sinh tinh mắt trên khán giả vẫn nhận ra cô — "
    "chính là cô gái giữ vị trí center vừa rồi. Nhiều người ngạc nhiên quay đầu, ánh mắt "
    "đuổi theo bóng dáng cô, mãi cho đến khi cô ra khỏi cửa phía sau nhà hát.",
    "“Vừa diễn xong đã đi, đi đâu thế? Vội vậy.”",
    "“Cô ấy thật sự rất xinh.”",
    "“Dáng người cũng đẹp nữa.”",
    "Cả một vùng khán giả quay đầu lại thậm chí còn thu hút cả mấy vị lãnh đạo nhà trường "
    "nhìn sang.",
    "Nhưng riêng vị ngồi giữa vẫn bất động thưởng thức tiết mục trên sân, bóng lưng sâu "
    "lắng trầm ổn, các ngón tay vẫn gõ đều đặn lên tay vịn ghế.",
    "Quả không hổ là ngài hiệu trưởng, khí thế đúng là vững chãi…… Vị trưởng khoa ngồi "
    "phía sau vẫn luôn quan sát hiệu trưởng, mặt đỏ bừng, lần nữa thầm khen trong lòng, "
    "ông ấy thật muốn được ngồi ngay cạnh ngài hiệu trưởng.",
    "Thời Linh ở lại phòng chuẩn bị, không vội thay đồ.",
    "Cô mở điện thoại, xem diễn đàn “Bạch Thanh Hạ”.",
    "Đến giờ, trong diễn đàn “Bạch Thanh Hạ” gần như toàn là bài mới gắn nhãn thời gian "
    "“vài phút trước”, “vài giây trước”, “vừa xong”.",
    "【Điểm danh, điểm danh!】",
    "【Lên tiếng đây, tranh thủ tích điểm kinh nghiệm.】",
    "【Tôi tuyên bố đây là nữ thần mới của khu Vãn Phong!】",
    "【Bạch Thanh Hạ, cái tên này hay thật đấy】",
    "【Lượt quan tâm ít thế, mới lập tài khoản à?】",
    "【Nhảy múa hay quá, tiết mục vừa rồi mắt tôi không rời nổi, cứ nhìn chằm chằm cô ấy "
    "suốt】",
    "Ở trên cùng còn có một bài chính Bạch Thanh Hạ vừa mới đăng, mới ba phút mà đã có hai "
    "ba chục lượt trả lời.",
    "【Chủ diễn đàn - Bạch Thanh Hạ: Kính xin mọi người đến sân vận động khu Vãn Phong "
    "ủng hộ buổi biểu diễn của Ban nhạc Dương Cốc, cảm ơn vô cùng!】",
    "……",
    "Ban nhạc Dương Cốc? Thời Linh nhớ lại nội dung tờ rơi đó, cô không khỏi tò mò vì sao "
    "một người nhút nhát hướng nội như Bạch Thanh Hạ lại tham gia vào kế hoạch nghe có vẻ "
    "điên rồ như vậy.",
    "Ban nhạc có liên quan gì đến cô ấy chứ?",
    "Thời Linh quay về giao diện【Bảng xếp hạng lượt quan tâm theo nhảy múa】, chỉ trong "
    "chốc lát, lượt quan tâm của diễn đàn “Bạch Thanh Hạ” đã tăng từ hơn 100 lên hơn 200.",
    "Trên bảng tăng trưởng toàn hạng mục, có lẽ đã lọt vào top mười.",
    "Còn gọi là “bảng tăng trưởng toàn hạng mục”, tên đầy đủ là:【Bảng xếp hạng tăng trưởng "
    "theo thời gian thực hằng ngày lượt quan tâm diễn đàn nghệ thuật toàn hạng mục Đại học "
    "Châu】",
    "Thời Linh lật tới bảng này, trong bảng gần một nghìn người cộng tất cả các hạng mục, "
    "diễn đàn “Bạch Thanh Hạ” quả nhiên trong mười mấy phút đã dùng tốc độ tăng theo thời "
    "gian thực “110 lượt” vọt lên hạng sáu.",
    "Hạng nhất tăng trưởng hiện vẫn là diễn đàn “Ban nhạc Vũ Lạc”, tốc độ tăng theo thời "
    "gian thực hằng ngày lên tới con số khủng “1058 lượt”.",
    "Có vẻ Ban nhạc Vũ Lạc đã biểu diễn ở đêm hội chào tân sinh viên khu Trần Hà rồi, tốc "
    "độ tăng từ khoảng “500” mỗi ngày thường lệ bỗng đạt hơn 1000 vào lúc này.",
    "Đúng là khi những hoạt động chính thức của trường bắt đầu, cuộc cạnh tranh về lượt "
    "quan tâm trở nên gay gắt bất thường, mấy thứ hạng đầu thậm chí còn là đấu giữa các "
    "thần tiên.",
    "Thời Linh phát hiện tốc độ tăng của mình hiện chỉ đứng thứ 8 trên bảng tăng trưởng "
    "toàn hạng mục…… dù cô vẫn là số một về lượt quan tâm hạng mục nhảy múa, mặc cho nền "
    "tảng lớn như vậy, tốc độ tăng vẫn chỉ xếp thứ tám.",
    "Hạng hai bảng tăng trưởng toàn hạng mục hiện là diễn đàn “Ban nhạc Dương Cốc”, tốc độ "
    "tăng lượt quan tâm theo thời gian thực của họ đạt “324”.",
    "Rốt cuộc năm người này đã làm gì trên sân vận động? Tăng nhanh thế?",
    "Chẳng lẽ kế hoạch của họ thành công rồi? Thời Linh thắc mắc trong lòng. Sân vận động "
    "khu Vãn Phong.",
    "Khi chọn bài, Lục Viễn Thu đã bàn với các thành viên, tất cả bài hát được chọn đều "
    "phải là bài có thể khuấy động không khí.",
    "Trịnh Bách Khoa từng nói, bài hát không chỉ phải có tác dụng khuấy động không khí, mà "
    "còn phải giữ chân người, rốt cuộc khán giả đâu phải bỏ tiền mua ghế, mấy người này "
    "xem náo nhiệt chán rồi muốn đi giữa chừng thì đi, chẳng lỗ gì.",
    "Chiếc xe tải dưới chân ban nhạc lúc này chính là một điểm thu hút tuyệt đối, ý tưởng "
    "của Lục Viễn Thu ra rất hay, nhờ có sự tồn tại của chiếc xe tải này, số khán giả đến "
    "ủng hộ tiếp theo chắc chắn sẽ nối dài không ngớt, nên phần còn lại hoàn toàn phụ thuộc "
    "vào việc lựa chọn bài hát có thể giữ chân những người này hay không.",
    "Biết đâu còn thật sự giành được cả nửa đại lễ đường, ít nhất cục diện mở đầu lúc này "
    "vô cùng thuận lợi.",
    "Còn về phần biểu diễn của ban nhạc, nhìn xuống dưới sân bấy nhiêu khán giả tập trung "
    "hết sức, khi thì hưng phấn reo hò, khi thì nhảy lên vỗ tay, Lục Viễn Thu cho rằng tiết "
    "mục của “Ban nhạc Dương Cốc” có lẽ không tính là đỉnh cấp, nhưng tuyệt đối đã đáp ứng "
    "được những điều kiện cơ bản mà một ban nhạc chuyên nghiệp cần có.",
    "Tôi chưa từng học qua âm nhạc viện, tiếng hát vẫn tạm được, lại còn được không ít "
    "người khen hay to tiếng, thật sự cảm tạ ông trời ban phước.",
    "Đã vậy thì tôi không khách khí nữa, lượt quan tâm lần này tôi nhất định phải ăn nên "
    "làm ra.",
    "Đạp lên Tinh Vệ, đấm vào Vũ Lạc, trở thành vua lượt quan tâm của ban nhạc trường năm "
    "2012!",
    "Rồi, cùng Bạch Thanh Hạ thuận lợi tốt nghiệp.",
    "Bài hát thứ hai 《Bỏ Trốn》",
    "“Đừng buồn nữa.”",
    "“Tôi đã nhìn thấy hy vọng.”",
    "“Em có còn dũng khí để theo tôi rời đi không.”",
    "Micro được gắn lại trên trụ, Lục Viễn Thu một tay nắm trụ đen, một tay cầm micro, cậu "
    "nhắm mắt hát bằng cả trái tim, cơ thể cũng nhịp theo tiết tấu bài hát.",
    "Tào Sảng ngồi trên nóc xe nghe rất nhập tâm, đột nhiên, anh kéo cặp kính râm xuống, "
    "mắt ngơ ngác nhìn về một hướng.",
    "Giọng hát của Lục Viễn Thu cũng chững lại ngay khoảnh khắc ấy.",
    "Khán giả đang tận hưởng tiếng hát, thấy Lục Viễn Thu dừng lại liền có chút thắc mắc, "
    "hơn nữa họ phát hiện Lục Viễn Thu đang ngơ ngác nhìn về phía sau họ.",
    "Khán giả đồng loạt quay đầu.",
    "Hóa ra trên sân vận động không biết từ lúc nào đã bước vào một cô gái mặc áo múa "
    "ballet xinh đẹp, hầu hết mọi người đã nhận ra đây chính là bà chủ của Mì Hạ Nhất "
    "Uyển.",
    "Bạch Thanh Hạ đứng yên tại chỗ, hai cánh tay thon thả khép trước người, chân đi đôi "
    "giày ballet màu be, chân mang vớ liền màu trắng tinh, trên người là chiếc váy thiên "
    "nga ôm sát xinh đẹp, hai bên đầu thậm chí còn đội cặp trang sức lông vũ trắng.",
    "Nhiều năm sau khi nhớ lại cảnh tượng này, Lục Viễn Thu từng nói, cậu thậm chí còn cảm "
    "thấy khoảnh khắc ấy Bạch Thanh Hạ đang mặc áo cưới chạy đến tìm cậu.",
    "Trước bấy nhiêu ánh mắt, Bạch Thanh Hạ cụp mắt xuống, lúc này lại không biết phải làm "
    "gì, can đảm chạy đến hình như chỉ đủ giữ lại được dọc đường thôi.",
    "“Chờ một lát, nhạc cũng dừng một lát.”",
    "Lục Viễn Thu nói vào micro, rồi lập tức nhảy xuống xe tải.",
    "Đám đông nhường ra, để lại ở giữa một lối nhỏ vừa một người đi qua, Lục Viễn Thu bước "
    "bên trong, mục tiêu rõ ràng tiến về phía Bạch Thanh Hạ sau đám người.",
    "“Khác hẳn với trang phục chúng ta tự mua thật đấy, đầu còn đội trang sức, xinh quá.” "
    "Lục Viễn Thu bước đến trước mặt cô, nhìn đánh giá cả bộ đồ của cô, rồi lại hỏi: “Em "
    "đến cổ vũ cho anh à?”",
    "Bạch Thanh Hạ khẽ gật đầu, nhỏ giọng nói với cậu: “Ừ, em vừa giữ vị trí center đấy "
    "nhé, sau khi nhảy xong có rất nhiều người chú ý đến em, em bảo họ đều đến ủng hộ anh, "
    "rồi em cũng tới.”",
    "Cô ngẩng đầu, trên mặt nở nụ cười e thẹn, giá như không có nhiều người lạ như vậy, nụ "
    "cười lúc này của cô có lẽ sẽ rực rỡ lắm.",
    "“Được, theo anh.”",
    "Lục Viễn Thu nói xong, trước mặt mọi người nắm lấy cô gái mặc áo múa ballet chạy về "
    "phía xe tải, Bạch Thanh Hạ một tay ôm ngực, nhanh chóng theo sát phía sau.",
]

# --------------------------------------------------------------------------
# 5. chapter headings that need a Vietnamese title
# --------------------------------------------------------------------------
TITLE_TEXT = {
    130: "Bánh tart trứng thật đáng yêu",
    138: "Công tử Trương có hình xăm bích đào [Bốn canh]",
    141: "Mặt trời và mặt trăng [Đại chương 4k]",
    143: "Giả! Giả! Đều là giả!",
    144: "Tự hào về người cha của mình",
    148: "Thanh niên văn nghệ và chị đại nóng bỏng [Bốn canh]",
    155: "Cậu thật không ăn sao? [Bốn canh]",
    158: "Mưa rơi sâu đến vậy [Ba canh]",
    162: "Cậu tỏ tình với Trịnh Nhất Phong",
    172: "Chủ đề tiếc nuối của tuổi thanh xuân",
    174: "Cậu biết làm mì không? Mì?",
    187: "Mùa đông",
    192: "Chàng trai áo trắng có hình xăm bích đào [Chúc mừng năm mới!]",
    200: "Giáng sinh, cây thông Noel và giấy ước",
    201: "Đến lượt thuyết trình rồi! [Ba canh]",
    203: "Tiếc nuối luôn gắn liền với tuổi thanh xuân",
    207: "Biên tập viên: Không đúng, chắc chắn là thiếu gì đó [Ba canh]",
    208: "Rốt cuộc là không đúng ở đâu",
    209: "Điều ước Giáng sinh",
    216: "Thiên nga trong đêm hội mừng năm mới [Đại chương 6k]",
    219: "Cặp đôi ăn ý [Ba canh]",
    222: "Ba cậu nam sinh đeo ba lô [Ba canh]",
    226: "Không cần em nữa rồi phải không?",
    229: "Quá khứ không thể đuổi theo, nhưng vẫn lấp lánh trong trí nhớ",
    232: "Tô Diệu Diệu đang giận",
    234: "Báo chiều Lô Thành gây tranh luận",
    245: "Hai người rốt cuộc đi tới bước nào rồi? [Ba canh]",
    246: "Mồng hai tháng hai, giao thừa",
    252: "Ảnh cả nhà",
    253: "Tiền mừng tuổi khổng lồ",
    266: "Cô ấy tại sao lại quan tâm Trịnh Nhất Phong đến thế",
    275: "Bạch Thanh Hạ bị ốm",
    276: "Tô Diệu Diệu phải đi rồi",
    282: "Mẹ cậu có người yêu chưa?",
    284: "Kết quả kỳ thi thử lần ba ra lò",
    285: "Trăng sáng soi đại giang",
    288: "Nhận phòng khách sạn",
    292: "Đừng bỏ em! [Hết tập 1]",
    296: "Nụ hôn tại buổi chụp ảnh tốt nghiệp",
    297: "Chọn trường đại học và nguyện vọng xét tuyển",
    298: "Việc làm hè",
    304: "Cuộc quân huấn kéo dài 18 ngày",
    319: "Học phần đổi mới đầu tiên",
    322: "Tới căn cứ",
    327: "Loạt kịch lớn về chuyện bênh vực người nhà",
    333: "Nhìn kìa! Nhìn kìa! Là kẻ địch!",
    337: "Thời đại thiếu nữ",
    340: "Cái đùi gà ngon nhất",
    352: "Đạt được giấy khen tiêu biểu",
    354: "Cho con đôi cánh, hãy dũng cảm bay nhé!",
    364: "Không sai, đúng là mùi đồ đã mặc",
    365: "Hiệu trưởng đích danh muốn gặp cậu",
    366: "Một thiếu phụ vẫn còn phong tình",
    379: "Liễu Vọng Xuân cũng tạm được",
    383: "Tôi sai rồi, cậu tha thứ cho tôi đi!",
    403: "Anh người mẫu à, tôi cũng muốn tiến bộ",
    410: "Chờ một lát, nhạc cũng dừng một lát",
    426: "Bạch Thanh Hạ: Anh vẫn luôn là người đẹp trai nhất trong các bạn nam!",
    432: "Ừ, tôi bắt đầu diễn rồi đây",
}

PREFIX_RES = [
    r"^##\s*第?\s*\d+\s*章\s*",
    r"^第\s*\d+\s*章\s*",
    r"^##\s*\d+\s*章\s*",
    r"^Chương\s*\d+\s*[:：]?\s*",
    r"^Chapter\s*\d+\s*[:：]?\s*",
    r"^(?:Cương|Điều|Chiêu|Đường|Tháng|Hành|Cơp|Cơng|Đoàn)\s*\d+\s*[:：]?\s*",
    r"^Đệ\s*\d+\s*chương\s*",
    r"^Hệ thống\s*\d+\s*Chương\s*",
    r"^định\s*\d+\s*chương\s*",
    r"^\d+\s*\.\s*",
    r"^\d+\s+",
]


def strip_prefix(s):
    s = s.strip()
    for pat in PREFIX_RES:
        ns = re.sub(pat, "", s, count=1)
        if ns != s:
            return ns.strip()
    return s


PUNCT = [("，", ", "), ("：", ": "), ("；", "; "), ("！", "!"), ("？", "?"),
         ("。", ". "), ("（", "("), ("）", ")"), ("、", ", "),
         ("【", "["), ("】", "]")]

MARKERS = [
    ("[Canh ba]", "[Ba canh]"), ("[Can ba]", "[Ba canh]"),
    ("[Ba chap]", "[Ba canh]"), ("[Ba căn]", "[Ba canh]"),
    ("[Ba chương]", "[Ba canh]"), ("[ba canh]", "[Ba canh]"),
    ("[Bốn chương]", "[Bốn canh]"), ("[Tứ chương]", "[Bốn canh]"),
    ("[Chương bốn]", "[Bốn canh]"), ("[Bốn chap]", "[Bốn canh]"),
    ("[Canh bốn]", "[Bốn canh]"),
]


def normalise(s):
    for a, b in PUNCT:
        s = s.replace(a, b)
    for a, b in MARKERS:
        s = s.replace(a, b)
    s = re.sub(r"(?<=\S)(\[(?:Ba canh|Bốn canh|Đại chương|Chúc mừng|Hết tập|"
               r"Chương thứ tư|Cập nhật))", r" \1", s)
    for _ in range(3):
        s = s.replace(":  ", ": ").replace(",  ", ", ").replace(".  ", ". ")
    return s.strip()


# --------------------------------------------------------------------------
# 6. `p&gt;` structural repair
# --------------------------------------------------------------------------
def fix_pgt(text):
    if "p&gt;" not in text:
        return text, 0, ""
    n = text.count("p&gt;")
    text = text.replace("p&gt;</p>", "</p>")
    text = text.replace("p&gt;<p>", "</p><p>")
    text = text.replace("p&gt;", "</p>")
    opens = len(re.findall(r"<p[ >]", text))
    closes = text.count("</p>")
    note = ""
    if closes > opens:
        m = re.search(r"(?:</p>\s*)+</body>", text)
        if not m:
            note = "cannot locate trailing </p> run"
        else:
            seg = m.group(0)
            keep = seg.count("</p>") - (closes - opens)
            if keep < 1:
                note = "negative keep"
            else:
                text = text[:m.start()] + "</p>" * keep + "\n  </body>" + text[m.end():]
    elif closes < opens:
        note = "missing %d </p>" % (opens - closes)
    return text, n, note


# --------------------------------------------------------------------------
# 7. per-file processing
# --------------------------------------------------------------------------
PARA_RE = re.compile(r"<p([^>]*)>(.*?)</p>", re.S)


def process(name, src_name, raw, orig_raw, report, fname):
    # normalise Unicode first: a handful of files store decomposed Latin
    # (e.g. `Lục` as L + u + combining dot), which breaks name matching.
    raw = unicodedata.normalize("NFC", raw)
    orig_text = raw

    # protect tokens that must survive the whole replacement pass untouched
    for keep, ph in PROTECT:
        raw = raw.replace(keep, ph)

    # ---- (a) Xia Xia: 小夏 -> Tiểu Hạ (files 137/169/215) ------------------
    if fname in XIA_HA_FILES:
        c = raw.count("Xia Xia")
        if c:
            raw = raw.replace("Xia Xia", "Tiểu Hạ")
            report["xia_%d" % fname] = c

    # ---- (b) paragraph level rewrites --------------------------------------
    spec = SPECIAL.get(fname, {})
    if spec:
        _ip = 0

        def _p2(m):
            nonlocal _ip
            attrs, inner = m.group(1), m.group(2)
            n = _ip
            _ip += 1
            if n in spec:
                exp, new = spec[n]
                if (exp not in inner) and (not re.search(exp, inner)):
                    raise SystemExit("PARA MISMATCH %s p%d: %r" % (fname, n, inner[:150]))
                if exp != new:
                    return "<p%s>%s</p>" % (attrs, new)
            return m.group(0)
        raw = PARA_RE.sub(_p2, raw)

    # ---- (c) chapter 410 full retranslation --------------------------------
    if fname == 410:
        cnt = [0]

        def _p410(m):
            attrs, inner = m.group(1), m.group(2)
            i = cnt[0]
            cnt[0] += 1
            if i < len(CH410):
                return "<p%s>%s</p>" % (attrs, CH410[i])
            return m.group(0)
        new = PARA_RE.sub(_p410, raw)
        if cnt[0] != len(CH410):
            raise SystemExit("ch410 paragraph count %d != %d" % (cnt[0], len(CH410)))
        raw = new

    # ---- (d) ch411 ambiguous name ------------------------------------------
    if fname == 411:
        cnt = [0]
        used = {}

        def _p411(m):
            attrs, inner = m.group(1), m.group(2)
            i = cnt[0]
            cnt[0] += 1
            if "Trịnh Tông Hạo" in inner:
                if i not in NAME411:
                    raise SystemExit("ch411 unexpected name at p%d" % i)
                used[i] = used.get(i, 0) + inner.count("Trịnh Tông Hạo")
                inner = inner.replace("Trịnh Tông Hạo", NAME411[i])
            return "<p%s>%s</p>" % (attrs, inner)
        raw = PARA_RE.sub(_p411, raw)
        report["ch411"] = used
        left = raw.count("Trịnh Tông Hạo")
        if left:
            raise SystemExit("ch411: %d occurrences outside <p>" % left)

    # ---- (e) p&gt; ---------------------------------------------------------
    raw, n_pgt, pgt_note = fix_pgt(raw)
    if n_pgt:
        report.setdefault("pgt", {})[fname] = (n_pgt, pgt_note)

    # ---- (f) ordered replacement table -------------------------------------
    before = raw
    for _pass in range(3):
        changed_pass = False
        for pat, repl in ORDER:
            if pat in raw:
                nxt = sub_one(raw, pat, repl)
                if nxt != raw:
                    raw = nxt
                    changed_pass = True
        if not changed_pass:
            break
    raw = normalise(raw)

    # ---- (g) heading + <title> --------------------------------------------
    m = re.search(r"<h2>(.*?)</h2>", raw, re.S)
    if m:
        old_h = m.group(1)
        if fname in TITLE_TEXT:
            text = TITLE_TEXT[fname]
        else:
            text = strip_prefix(old_h)
            if CJKC.search(text) or not text:
                raise SystemExit("HEADING NEEDS TRANSLATION %s: %r" % (fname, text))
            text = normalise(text)
        tm = re.search(r"<title>(.*?)</title>", orig_raw, re.S)
        num = re.match(r"第(\d+)章", tm.group(1).strip()) if tm else None
        n = num.group(1) if num else ""
        h2 = ("Chương %s: %s" % (n, text)) if n else text
        h2 = normalise(h2)
        raw = raw[:m.start()] + "<h2>%s</h2>" % h2 + raw[m.end():]
        report.setdefault("h2", {})[fname] = h2

    m = re.search(r"<title>(.*?)</title>", raw, re.S)
    if m:
        h2v = report.get("h2", {}).get(fname)
        if h2v:
            raw = raw[:m.start()] + "<title>%s</title>" % h2v + raw[m.end():]

    # ---- sanity ------------------------------------------------------------
    if CJKC.search(raw):
        ctx = []
        for mm in re.finditer(r"[一-鿿]+", raw):
            ctx.append(raw[max(0, mm.start() - 60):mm.end() + 60])
            if len(ctx) >= 8:
                break
        report.setdefault("cjk_left", {})[fname] = ctx
    if "p&gt;" in raw:
        report.setdefault("pgt_left", []).append(fname)

    for keep, ph in PROTECT:
        raw = raw.replace(ph, keep)

    return raw, raw != orig_text


def main():
    zs = zipfile.ZipFile(EPUB)
    src = zipfile.ZipFile(ROOT + r"\重生：开局逮到高冷校花超市偷窃.epub")
    out = {}
    report = {"h2": {}, "changed": 0}
    problems = []
    for i in range(LO, HI + 1):
        name = "EPUB/chap_%04d.xhtml" % i
        raw = zs.read(name).decode("utf-8", "ignore")
        orig = src.read(name).decode("utf-8", "ignore")
        try:
            new, changed = process(name, name, raw, orig, report, i)
        except SystemExit as e:
            problems.append((i, str(e)))
            new, changed = raw, False
        out[name] = new
        if changed:
            report["changed"] += 1

    print("files changed : %d / %d" % (report["changed"], HI - LO + 1))
    if problems:
        print("PROBLEMS (%d):" % len(problems))
        for i, e in problems:
            print("  %s: %s" % (i, e))
    if report.get("cjk_left"):
        print("CJK LEFT:", report["cjk_left"])
    else:
        print("CJK in body/h2/title : 0")
    if report.get("pgt_left"):
        print("p&gt; LEFT:", report["pgt_left"])
    if report.get("pgt"):
        print("p&gt; repaired:", report["pgt"])
    if report.get("ch411"):
        print("ch411:", report["ch411"])
    if report.get("xia_137") or report.get("xia_169") or report.get("xia_215"):
        print("xia->Tiểu Hạ:", {k: v for k, v in report.items() if k.startswith("xia_")})

    if not APPLY:
        import os
        d = os.path.join(os.environ.get("TEMP", "."), "migrated_epub")
        os.makedirs(d, exist_ok=True)
        for n, txt in out.items():
            with open(os.path.join(d, n.replace("/", "_")), "w", encoding="utf-8") as fh:
                fh.write(txt)
        with open(os.path.join(d, "_h2.tsv"), "w", encoding="utf-8") as fh:
            for i in range(LO, HI + 1):
                fh.write("%d\t%s\n" % (i, report["h2"].get(i, "")))
        print("(dry run - preview written to %s)" % d)
        return

    bak = EPUB + ".bak-pre-migrate.epub"
    shutil.copy2(EPUB, bak)
    print("backup ->", bak)

    buf = {}
    with zipfile.ZipFile(EPUB) as z:
        for n in z.namelist():
            buf[n] = z.read(n)
    for n, txt in out.items():
        buf[n.encode("utf-8") if False else n] = txt.encode("utf-8")
    with zipfile.ZipFile(EPUB, "w", zipfile.ZIP_DEFLATED) as z:
        for n in buf:
            z.writestr(n, buf[n])
    print("written:", EPUB)


if __name__ == "__main__":
    main()
