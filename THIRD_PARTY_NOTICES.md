# 第三方组件许可声明

SecondBrain 本身采用 [MIT License](LICENSE)。分发的可执行程序会随附下列第三方组件，
其版权归各自作者所有，许可文本以各组件自带的 LICENSE / 包元数据为准。

本项目选用依赖时刻意避开了 GPL/AGPL 系列，全部为宽松许可（MIT / Apache-2.0 / Unlicense），
因此不会对本项目的授权方式产生额外约束。

## 后端（Python）

| 组件 | 用途 | 许可 |
|---|---|---|
| FastAPI / Pydantic / uvicorn / httpx | Web 服务与 HTTP | MIT |
| jieba | 中文分词 | MIT |
| trafilatura | 网页正文提取 | Apache-2.0 |
| yt-dlp | 视频/音频抓取 | Unlicense |
| faster-whisper / ctranslate2 / onnxruntime | 语音转写与向量 | MIT |
| av (PyAV) / pywebview / winotify | 媒体解码 / 桌面窗口 / 通知 | 见各包自带声明 |

## 前端（Node）

| 组件 | 用途 | 许可 |
|---|---|---|
| Vue / Vue Router | UI 框架 | MIT |
| Vditor | Markdown 编辑器 | MIT |
| Vite / ESLint / Prettier | 构建与代码质量 | MIT |
| TypeScript | 类型检查（仅开发期） | Apache-2.0 |

## 模型与二进制

- yt-dlp / ffmpeg 若随 exe 一并分发，其许可与源码获取方式以各自官方说明为准。
- faster-whisper 的模型权重由用户在首次使用时自行下载，不包含在本仓库或本程序包内；
  权重许可由模型发布方决定。

> 若你重新分发本程序，请一并保留本文件与各依赖自带的许可文本。
