"""把「平台适配器能力」从设置区挪到功能区（新增第 6 个 Tab）。

- 设置区·系统 少了 396px 的只读展示卡 → 一屏放得下
- 功能区本来就是"能力与调控"的地方
"""

import pathlib
import re

# ============================================================
# ① 从 SettingsView 删掉适配器能力卡片
# ============================================================
p = pathlib.Path("frontend/src/views/SettingsView.vue")
s = p.read_text(encoding="utf-8")

start = s.find("      <!-- 适配器 -->")
end = s.find("      <!-- ASR -->")
assert start != -1 and end != -1 and start < end, "适配器卡片边界没找到"
block = s[start:end]
s = s[:start] + s[end:]
p.write_text(s, encoding="utf-8")
print(f"✓ 设置区已移除适配器卡片（{len(block)} 字符）")

# 提取卡片内容，供功能区使用（去掉最外层 card 包装里的注释）
card_inner = block
card_inner = card_inner.replace("      <!-- 适配器 -->\n", "")
card_inner = card_inner.replace("      </div>\n", "", 1) if False else card_inner
pathlib.Path("C:/Temp/adapter_card.html").write_text(card_inner, encoding="utf-8")
print("  → 卡片内容已暂存")

# ============================================================
# ② 功能区加「能力」Tab
# ============================================================
p = pathlib.Path("frontend/src/views/FeaturesView.vue")
s = p.read_text(encoding="utf-8")

# Tab 按钮
old = """        <button class="tab" :class="{ active: section === 'maintenance' }" @click="section = 'maintenance'">
          维护与诊断
        </button>"""
new = """        <button class="tab" :class="{ active: section === 'maintenance' }" @click="section = 'maintenance'">
          维护与诊断
        </button>
        <button class="tab" :class="{ active: section === 'adapters' }" @click="section = 'adapters'">
          平台能力
        </button>"""
assert old in s, "维护与诊断 Tab 没找到"
s = s.replace(old, new)

# 内容：插到「维护与诊断」区块之后（</template> 前）
# 先找维护与诊断 section 的结束
m = re.search(r'<div v-if="section === \'maintenance\'">[\s\S]*?\n      </div>\n', s)
if not m:
    # 换个方式：找最后一个 section 分支
    m = re.search(r'v-if="section === \'maintenance\'"[\s\S]*?\n      </div>\n      </div>\n', s)
assert m, "维护与诊断区块没找到"

adapter_block = pathlib.Path("C:/Temp/adapter_card.html").read_text(encoding="utf-8")
# 去掉多余的 </div>（原卡片自闭合）
adapter_block = adapter_block.rstrip()
if adapter_block.endswith("</div>"):
    adapter_block = adapter_block[: -len("</div>")].rstrip()

insert = (
    '\n      <!-- 平台能力（从设置区搬来：各平台的支持等级一览） -->\n'
    '      <div v-if="section === \'adapters\'">\n'
    + adapter_block
    + "\n      </div>\n"
)
s = s[: m.end()] + insert + s[m.end() :]

p.write_text(s, encoding="utf-8")
print("✓ 功能区已加「平台能力」Tab")
