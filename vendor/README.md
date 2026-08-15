# vendor/ — 本地 three.js

本目录是 vendored 进仓库的 [three.js](https://threejs.org/) 副本，供
`feature_picker.py` 生成的离线 HTML 预览使用。**运行时不再从任何 CDN 下载**。

## 版本与许可
- **版本**：three.js **r160 / v0.160.0**
  （固定自 CDN 的该版本，入库即锁定，不随上游自动升级）
- **许可**：**MIT**
  - 文件头：`Copyright 2010-2023 Three.js Authors`
  - `SPDX-License-Identifier: MIT`
- **来源**：从 three.js 官方 CDN 固定版本（three@0.160.0）抓取后 vendored 入库；
  仓库即唯一可信源。

## 文件清单（pinned SHA-256）
`feature_picker._ensure_vendor()` 在生成预览前会对以下文件做 SHA-256 校验；文件缺失或
被篡改（hash 不匹配）即拒绝运行并明确报错，避免被植入恶意 JS。`vendor/` 必须随仓库提交，
重新获取时请从可信来源同步，**切勿**在运行时从 CDN 下载。

| 文件 | SHA-256 |
|------|---------|
| `three.module.min.js` | `3e690ac7d180b0aadf0891bea39eec643e29e2d3e75c99b18689518665f69ba6` |
| `jsm/controls/OrbitControls.js` | `5a44a9e86a2a0fb11933eed69bc2cd33c76a496854c1aed6ed776efa87d7b064` |
| `jsm/loaders/STLLoader.js` | `896d006a48b8f125385a485ccae154dadee801a953f0b45ceffe7ddd8a29ca93` |

> 上表与代码 `feature_picker._VENDOR_SHA256` 中 pin 的值一致，且与当前仓库文件的实际
> SHA-256 校验通过。升级 three.js 版本时，需同步更新 `feature_picker.py` 里的 pin 值与本表。
