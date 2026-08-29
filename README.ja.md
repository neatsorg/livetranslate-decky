# LiveTranslator-kun

[English](./README.md) | [日本語](./README.ja.md)

画面をキャプチャし、OCRで文字を認識して、リアルタイムで翻訳結果をオーバーレイ表示するツールです。現在は、英語から日本語への翻訳に対応しています。

AIコーディングを使用しています。他のコードを流用した部分はないですが、気になさる方は使用をお控えいただければと思います。

[Video Demo](https://www.youtube.com/watch?v=wTeWC3wXl9k)

## はじめに

- **[Windows版](docs/WINDOWS.ja.md)** - スタンドアロンアプリです。Python不要。
- **Steam Deck / Linux版** - [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) 用プラグインです。Gamescope/PipeWire経由で画面をキャプチャします。多くの機能は実機のDeckでなくても、標準的なLinux環境で動作します。(このブランチはWindows版とのマージ作業中のため、Linux版の最新のインストール手順は`main`ブランチを参照してください。)

## 機能

- リアルタイム自動翻訳 - ゲーム画面上に表示された文章を自動で検出し、原文の座標に翻訳を表示します。画面内で文章の位置が変わったり、スクロールしたり、画面そのものが切り替わった場合でも、追随して翻訳を試みます。画面表示をリフレッシュして翻訳し直したり、一時的に翻訳機能を停止することもできます。
- 矩形キャプチャモード - ゲームに応じて字幕などの表示エリアのみに限定した自動翻訳ができます。
- 直近キャッシュ - 同じ文章が何度も表示される局面で、再OCRや再翻訳を行わず即時的な表示ができます。

## 拡張性

- マルチ翻訳エンジン：Google Cloud Translate、DeepL、Gemini AI、Google翻訳、Ollama使用によるAIモデル、などを選択可能。
- キーバインド：画面の更新、一時停止と再開、両方の機能を、キーボード、マウスボタン、ゲームパッドなどに割り当てられます。各キーバインドは、長押しやキー同時押しに対応。Windows版のデフォルトは**F9**(単押しで更新、長押しで一時停止/再開)。

## 開発

- Windows版: [docs/BUILDING_WINDOWS.md](docs/BUILDING_WINDOWS.md) を参照(英語)
- Steam Deck / Linux版: `main`ブランチの`docs/BUILDING.md`を参照

## サポート

このソフトがどれだけ使われるかわからないため、不具合の報告、サポートの依頼、または要望なども、どこまであるかわからないですし、対応できるかもわかりません。が、何かあったら [Issues](../../issues) からお願いします。

[Ko-fi](https://ko-fi.com/neatsorg)で支援することもできます。

## もし開発が継続した場合に追加する機能

- **ゲームを一時停止＆矩形選択範囲を翻訳**：ある意味、先祖返り的な機能。めざせPCOT
- **多言語対応**：日->英の翻訳のほか、ソース言語とターゲット言語を増やす
- **ゲーム以外への対応**：わりとできそう。でも動画字幕をライブ翻訳する等は今の速度だと無理です。

## ありそうな質問と答え

- **xxxxxというゲームに対応してほしい**：個別対応するソフトではないので、無理です。別ゲームでも同種のケースが多い、という場合には対応する可能性があります。
- **翻訳が弱い**：このツールは現状、OCRと翻訳をすべて外部に投げています。なので、そいつらが強くなるのを待とう。
- **翻訳が遅い**：高速化にはまだ余地があると思います。これは、もし開発が継続するなら、やる可能性が高いです。
- **Macに移植してほしい**：有益さが相対的に小さく、関心が持てないです。
- **音声を翻訳してほしい**：負荷が大きいのであまり関心が持てないです。強力な外部APIなどがあればやってみたいかも。
- **動かない**：ご使用の環境やゲームや状況をすごく詳細に説明していただければ
わかるかもしれませんが、保証はできません。また、あなたの環境が特殊すぎると対応はさらに難しいです。

## 謝辞

- [Valve](https://www.valvesoftware.com/) Steam Deckという素晴らしい機械、そしてGamescopeを作ってくれた。思想に共感する
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) Steam Deckの可能性を、ものすごく広げた。思想に共感する
- [Decky-Translator](https://github.com/cat-in-a-box/Decky-Translator) Decky Loaderでこんなことができるのか！と感動したよ。設計面もとても参考にさせてもらった
- [PlayTranslate](https://github.com/dominostars/playtranslate) このプレイ動画を見て、すごい！うらやましい！Steam Deckでも似たようなことをやってみよう！と思ったよ。だから僕のコードは、関数名などにその名残がある（もちろんコードは流用してないです😉）。しかし元のアイデアはこのひとのものです。すごい体験をありがとう
- [Google](https://google.com/) 翻訳、Gemini APIなどあらゆる面で
- [Chromium Projects](https://www.chromium.org/) Chrome Screen AI、すごい

## ライセンス

[GPLv3](LICENSE)

Windows版には複数のサードパーティライブラリ(PySide6、DXcam、pywin32、pywinrtなど)が同梱されています。それぞれのライセンスは[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)を参照してください(英語)。
