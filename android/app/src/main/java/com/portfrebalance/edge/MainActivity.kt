package com.portfrebalance.edge

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * 앱 = 파이썬 에이전트(127.0.0.1) + WebView.
 *
 * A2(Termux)와 **같은 server.py·같은 index.html**을 쓴다. APK가 되면서 달라지는 건 딱 둘:
 *   · OCR 엔진이 RapidOCR → ML Kit (agent/ocr.py의 어댑터 계약에서 끊긴다)
 *   · 바인드 주소가 0.0.0.0 → 127.0.0.1 (앱이 API를 Wi-Fi에 열면 안 된다)
 */
class MainActivity : AppCompatActivity() {

    private val port = 8899
    private lateinit var web: WebView
    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    // 스크린샷 선택 — photo picker / SAF 라 저장소 권한이 필요 없다(pickerIntent 참고).
    private val pickFiles = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        filePathCallback?.onReceiveValue(
            WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        )
        filePathCallback = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // index.html은 빌드 시 레포 루트에서 assets로 복사된다(사본을 커밋하지 않는다).
        // server.py가 파일 경로를 읽으므로 filesDir로 펼친다.
        val indexFile = File(filesDir, "index.html")
        assets.open("index.html").use { input ->
            indexFile.outputStream().use { input.copyTo(it) }
        }

        if (!Python.isStarted()) Python.start(AndroidPlatform(this))

        // 블로킹 서버 — 반드시 별도 스레드.
        thread(isDaemon = true) {
            Python.getInstance().getModule("android_main").callAttr(
                "start",
                indexFile.absolutePath,
                File(filesDir, "data").absolutePath,
                port
            )
        }

        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true      // 앱이 localStorage를 쓴다
            webViewClient = WebViewClient()
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(
                    view: WebView?,
                    callback: ValueCallback<Array<Uri>>?,
                    params: FileChooserParams?
                ): Boolean {
                    filePathCallback?.onReceiveValue(null)
                    filePathCallback = callback
                    pickFiles.launch(pickerIntent(params) ?: return false)
                    return true
                }
            }
        }
        setContentView(web)

        // 서버가 뜰 때까지 기다렸다가 로드한다(콜드 스타트에 수 초 걸린다).
        thread(isDaemon = true) {
            val url = "http://127.0.0.1:$port/"
            repeat(120) {
                if (healthOk(url + "health")) {
                    runOnUiThread { web.loadUrl(url) }
                    return@thread
                }
                Thread.sleep(500)
            }
            runOnUiThread {
                web.loadData("<h3>에이전트가 뜨지 않았습니다</h3>", "text/html; charset=utf-8", null)
            }
        }
    }

    /**
     * 웹의 두 업로드 진입을 그대로 재현한다 — 계약이 갈리면 APK만 다르게 동작한다.
     *   · accept="image/\*" (드롭존)  → createIntent() 그대로. 사진 그리드.
     *   · accept 없음 (파일 앱에서 선택) → ACTION_OPEN_DOCUMENT. SAF 의 DISPLAY_NAME 은 원본
     *     파일명이라 'Screenshot_…_앱이름.jpg' 가 살아 온다(photo picker 는 미디어 ID를 준다).
     * OPEN_DOCUMENT 도 SAF 라 READ_MEDIA_IMAGES 같은 저장소 권한은 여전히 필요 없다.
     */
    private fun pickerIntent(params: WebChromeClient.FileChooserParams?): Intent? {
        val types = params?.acceptTypes?.filter { it.isNotBlank() } ?: emptyList()
        val wantsAnyFile = types.isEmpty() || types.any { !it.startsWith("image/") }
        return if (wantsAnyFile) {
            Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("*/*")
                .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        } else {
            params?.createIntent()
        }
    }

    private fun healthOk(url: String): Boolean = try {
        (URL(url).openConnection() as HttpURLConnection).run {
            connectTimeout = 1000; readTimeout = 1000
            responseCode == 200
        }
    } catch (e: Exception) {
        false
    }

    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }
}
