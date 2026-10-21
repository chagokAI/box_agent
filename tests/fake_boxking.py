"""boxking 목록 구조를 흉내 낸 로컬 테스트 서버 (상품 값은 2026-10-20 boxking 목록 1페이지에서 확인한 실제 값)"""
import http.server, sys
from urllib.parse import urlparse, parse_qs

ROWS = [  # goodsNo, name, price
    (241, "택배박스 무지 기본 포장 상자 골판지 박스 모음", "26,900"),
    (390, "202호 (200 x 150 x 100) B골 160매", "32,100"), (42, "034호 (400 x 310 x 210) A골 33매", "30,900"),
    (38, "028호 (350 x 250 x 150) A골 55매", "33,700"), (50, "042호 (460 x 350 x 230) AB골 26매", "49,700"),
    (258, "176호 (400 x 300 x 200) B골 54매", "39,900"), (64, "056호 (600 x 450 x 330) AB골 16매", "51,200"),
    (465, "E-16호 (300 x 200 x 150) B골 105매", "36,900"),
    (121, "074호 (520 x 450 x 400) A골 20매", "51,200"), (57, "049호 (500 x 400 x 300) AB골 19매", "52,900"),
    (220, "158호 (600 x 400 x 250) AB골 19매", "51,200"), (425, "238호 (450 x 320 x 300) A골 17매", "25,600"),
    (48, "040호 (450 x 320 x 200) A골 27매", "28,700"), (252, "170호 (220 x 190 x 90) B골 140매", "34,500"),
    (453, "E-4호 (260 x 180 x 150) B골 132매", "38,200"),
]
PAGES = {1: ROWS[:8], 2: ROWS[8:]}
ROBOTS = """User-agent: mj12bot
User-agent: SemrushBot
User-agent: ClaudeBot
User-agent: GPTBot
Disallow: /

User-agent: Googlebot
User-agent: bingbot
Disallow: /admin/
Disallow: /config/
Disallow: /data/
Disallow: /module/
Disallow: /tmp/
Crawl-delay: 10
"""

def item(no, name, price, i):
    cost = f'<del class="item_cost">{int(price.replace(",",""))+5000:,}원</del>' if i % 3 == 0 else ""
    return f'''<li><div class="item_cont">
 <div class="item_photo_box"><a href="../goods/goods_view.php?goodsNo={no}"><img src="/data/goods/{no}.jpg" alt="{name}"></a></div>
 <div class="item_info_cont"><div class="item_tit_box"><a href="../goods/goods_view.php?goodsNo={no}"><strong class="item_name">{name}</strong></a></div>
 <div class="item_money_box">{cost}<strong class="item_price"><span>{price}원</span></strong></div>
 <div class="item_mileage">적립 300원</div></div></div></li>'''

def page_html(p):
    lis = "".join(item(*r, i) for i, r in enumerate(PAGES.get(p, [])))
    nav = "".join(f'<li><a href="./goods_list.php?page={n}&cateCd=011">{n}</a></li>' for n in PAGES if n != p)
    return f"<html><body><div class='goods_list'><ul>{lis}</ul></div><div class='pagination'><ul>{nav}</ul></div></body></html>"

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/robots.txt":
            body = ROBOTS
        elif u.path == "/goods/goods_list.php":
            body = page_html(int(parse_qs(u.query).get("page", ["1"])[0]))
        else:
            self.send_response(404); self.end_headers(); return
        b = body.encode(); self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(b)

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
