← [メインのREADMEに戻る](../README.ja.md)

# LiveTranslator-kun (Steam Deck / Linux版)

[Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) 用プラグインです。Gamescope/PipeWire経由で画面をキャプチャします。

## 動作環境

- Steam Deck (SteamOS) - その他のgamescopeベースのLinuxハンドヘルド/デスクトップでも実験的に動作します。詳細は [非Deck Linux環境での利用](#非deck-linux環境での利用) を参照してください。
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) がインストール済みであること
- OCR/翻訳エンジン用コンテナのための distrobox + podman(下記[セットアップ](#セットアップ)で自動インストールされます)
- オンデバイスOCRモデルの初回ダウンロード、およびクラウド翻訳エンジン利用時にインターネット接続が必要です

## インストール

1. Steam Deckに、[Decky Loader](https://decky.xyz)をインストールします。インストール方法は[詳細な説明が公式サイトにある](https://deckyloader.org/guide/how-to-install-decky-loader-steam-deck)ので読むとよいです。英語がわからない場合は[Google翻訳など](https://translate.google.com/?op=websites)を使って翻訳して読みましょう。
2. [ここをクリックして本ソフトの最新版をダウンロードしてください。](../../../releases/latest)
3. Steamのクイックアクセスメニューで電源プラグのマークのアイコンを押し、1.でインストールしたDecky Loaderの設定を開く。
4. Decky Loaderの設定が開いたら、左のメニューで**一般**を選択。一般設定の下のほうにある**開発者モード**スイッチを有効にする。
5. 新たに「プラグイン」「開発者」「テスト」などのメニューが現れるので、**開発者**を選ぶ。**ZIPファイル**のボタンを押し、ダウンロードしたファイルを開くとインストールできます。または**URL**のボタンを押して、最新版のzipファイルのURLを入力することでもインストール可能です。

## セットアップ

このプラグインはOCR/翻訳エンジンを小さなコンテナ内で実行するため、初回のみセットアップが必要です。

1. このプラグインのメニューから**OCR Settings**ボタンを押します。
2. 表示された画面で**Set Up OCR Environment** を押します。distrobox/podmanが未導入であればインストールされ、コンテナが作成され、エンジンに必要なPythonパッケージを導入します。途中で失敗しても(通信の一時的な失敗など)再度押せば安全にやり直せます。セットアップログはその場でリアルタイムに表示されます。ボタンの上に小さくdistrobox:installedおよび container:createdと表示されればインストール完了です。
3. デフォルトのOCRエンジン(Chrome Screen AI)について、ダウンロード作業が必要です。2.の作業を行った画面の最下段にある**Download**を押すとダウンロードされます。ボタンの上に小さくInstalledと表示されれば準備完了です。

以下は必要な場合のみ:

- **クラウドAPIキー**: Gemini AI、Google Cloud Translate、DeepLを使う場合。プラグインのメニューで**Translation Settings**ボタンを押すと、翻訳エンジンの選択画面が表示されます。有料のエンジンには、基本的にAPIキーが必要です。ここで入力します。なお、作者としてはGoogle Cloud Translateを最もおすすめします。
- **Ollama**: 翻訳エンジンをOllamaに設定する場合。ローカルまたはLAN上のOllamaサーバーを指定してください。プラグイン自体はOllamaのインストールや起動は行いません。なお、これを設定するUI欄はありません。プラグインのデータディレクトリ(Deck上では`~/homebrew/data/PlayTranslate/`)に`translate_url.txt`というファイルを作成し、サーバーの`http://host:port/translate`形式のURLを書き込むか、Steam起動前に`PLAYTRANSLATE_TRANSLATE_URL`環境変数を設定してください。どちらも未設定の場合は`http://127.0.0.1:8787/translate`が既定値として使われます。

## 使い方

1. プラグイン設定画面で**Start Capture**ボタンを押すと自動翻訳が開始します。
2. キーバインド設定されたキーを押すと、字幕のリフレッシュ（デフォルト：L4）や一時停止／再開の切り替え（デフォルト：L4長押し）ができます。
3. 一時停止中に、画面タップモード起動キー（デフォルト：L4＋L2）を押しながら画面を長押しすると、その部分にある文章の翻訳だけを画面下に表示します。
4. キーバインドの変更は、設定画面の**Key Bindings**ボタンを押すと行えます。
5. **Stop Capture**を押すとリアルタイム翻訳を終了します。このツールはゲーム画面かどうかを判断して翻訳を表示するわけではないので、翻訳が不要な状況では一時停止するか、または設定画面の**Stop Capture**を押してプラグインを終了しましょう。

### 矩形キャプチャモード

1. 自動翻訳を開始する際、**Start Capture**のかわりに**Start Region Mode**をクリックすると、画面内の特定の領域に対してのみの自動翻訳が開始されます。文章の表示される位置が常に同じであるゲームや、画面全体を翻訳されるとうっとおしい、という場合に利用してください。
2. 翻訳する領域の指定は、設定画面の**Region Mode Config**ボタンを押すことで行えます。

## トラブルシューティング

**何も検出されない・翻訳が表示されない**
設定画面のCapture Control欄に「running」と表示されているか確認してください。またOCR環境のセットアップが済んでいない場合には、OCRタブにその旨が表示されます。上記の[セットアップ](#セットアップ)を参照してください。

**翻訳エンジンがAPIキーのエラーを表示する**
設定画面に、該当プロバイダのキーが正しくコピーされているか確認してください。Gemini、Google Cloud Translate、DeepLはそれぞれ別のキーを使用します。

**OCR環境のセットアップが途中で失敗する**
**Set Up OCR Environment** を再度押してください。冪等な処理なので、続きからやり直されます。それでも失敗する場合は、OCRタブに表示されるセットアップログで実際のエラー内容を確認してください。

### 非Deck Linux環境での利用

本プラグインは主にSteamOS/Deckを対象に開発・検証されていますが、非Deck Linux環境(CachyOS上でのgamescope + Decky Loader)でも動作を確認済みです。ゲームパッド/キーボード入力まわりにいくつか注意点があります。

- **Steam Deckと同等の環境にするため、gamescopeが必須です**。これはVALVEが開発した、waylandプロトコルを扱うマイクロコンポジタです。ゲーム用にチューンされた豊富な機能が使えます。Steam Deckや、SteamOS等のゲーム系OSが搭載されたLinuxデスクトップに導入されています。gamescopeを経由してSteamを動作させ、映像をPipeWireに出力することで、ゲームの映像をストリームとしてキャプチャし、利用できます。すごいです。しかもgamescopeはmangohudの機能を統合しているので、これを使って字幕を出そうというのが、このソフトの最初のアイデアでした。  
インストール方法はお使いのディストリビューションによって異なります。SteamOSと同じArch系だと以下のようにして入れる場合が多いでしょう。
```bash
sudo pacman -S gamescope
```
- **Decky Loaderは各プラグインのバックエンドを、追加グループなしの非特権ユーザーとして実行します**。そのため、ユーザーを`input`グループに追加しても`/dev/hidraw*`/`/dev/input/event*`が読み取れないままの場合があります。キー割り当てでコントローラー/キーボードが検出されない場合は、以下のような緩めのudevルールを追加してください。
  ```bash
  printf '%s\n%s\n' \
    'SUBSYSTEM=="hidraw", MODE="0666"' \
    'SUBSYSTEM=="input", KERNEL=="event*", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-livetranslate-hidraw.rules
  sudo udevadm control --reload
  sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=input
  sudo systemctl restart plugin_loader
  ```
  これによりこれらのデバイスがホスト上で誰でも読み書き可能になります。シングルユーザーのデスクトップであれば妥当なトレードオフですが、共有マシンで適用する際は理解した上で行ってください。
- **`xpad`カーネルドライバに紐づくUSBゲームパッド**(多くの有線Xboxコントローラー)はhidrawノードを持たないため、キー割り当てはこれらのデバイスに対してはplain evdevにフォールバックします。デジタルボタンのみ対応し、アナログトリガーは非対応です。
- **オーバーレイ(このプラグイン自身の設定画面)を閉じると、ゲームウィンドウをマウスでクリックするまでゲーム側がゲームパッド入力を受け取れなくなることがあります**。これはgamescopeを直接実行するデスクトップLinux環境でのみ確認されており、実機のDeckでは確認されていません。プラグイン側での修正方法は今のところありません。
- **画面タップ翻訳が機能しません**。画面タップ翻訳の起動キーを押してから画面をクリックしても翻訳が表示されず、以後はマウスでクリックするまでゲームウィンドウにフォーカスが戻らないことがあります。前記の不具合と連動している可能性が高いです。
- **wayland系のコンポジタを使っている状態でgamescopeをネスト実行すると、ゲームパッド等の入力やオーバーレイ表示に不具合が生じる場合があります**。gamescopeは既存のディスプレイサーバ／デスクトップ環境上でネストして実行できます。しかしDecky Loaderの動作に不具合が出る可能性を否定できません。個人的におすすめなのは、埋め込みモードでgamescopeを実行することです。つまりウィンドウマネージャ（ログイン画面）でgamescopeを選択し、Steam Deckのような、専用の全画面Steam UIで端末を起動するわけです。これはデスクトップから起動したSteamでBig Pictureモードに移行した状態とは異なります。背後で別のコンポジタが動いていない、不具合が発生しにくい状況です。

## ソースからのビルド

ソースからのビルドについては[BUILDING.md](BUILDING.md) を参照してください(英語)。
