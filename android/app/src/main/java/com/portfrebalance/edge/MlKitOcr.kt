package com.portfrebalance.edge

import android.graphics.BitmapFactory
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.korean.KoreanTextRecognizerOptions
import org.json.JSONArray
import org.json.JSONObject

/**
 * ML Kit 텍스트 인식 → `agent/ocr.py`의 어댑터 계약 JSON.
 *
 *   [{"text","x","y","w","h","conf"}]   x,y = 좌상단, 원본 픽셀 좌표
 *
 * 이 클래스가 존재하는 이유는 하나다: **bind.py 이하를 손대지 않기 위해서.**
 * 검증(Orin)은 RapidOCR로 파리티를 통과했고, 폰에서는 엔진만 ML Kit으로 바뀐다.
 *
 * 두 가지를 정직하게 적어둔다 —
 *  1) **conf가 없다.** ML Kit 공개 API는 요소별 신뢰도를 노출하지 않는다. 1.0을 채운다.
 *     bind.py가 conf를 쓰지 않는 것을 확인했으므로(현재 참조 0곳) 계약 위반이 아니다.
 *  2) **박스 단위가 다르다.** RapidOCR은 구절 단위 검출 박스를 낸다. ML Kit은
 *     Block > Line > Element 계층이고 Line이 가장 가깝다. 이 차이가 ML Kit 스왑의
 *     **유일한 실질 위험**이라 `granularity`를 노브로 남긴다 — 기기에서 재서 고정한다.
 */
object MlKitOcr {

    private val recognizer by lazy {
        TextRecognition.getClient(KoreanTextRecognizerOptions.Builder().build())
    }

    /**
     * @param imageBytes  JPEG/PNG 바이트
     * @param granularity "line"(기본) | "element" | "block"
     * @return 계약 JSON 문자열
     *
     * **블로킹이다**(Tasks.await). 메인 스레드에서 부르면 안 된다 —
     * 파이썬 HTTP 서버 스레드에서 호출되므로 문제없다.
     */
    @JvmStatic
    @JvmOverloads
    fun recognize(imageBytes: ByteArray, granularity: String = "line"): String {
        val bmp = BitmapFactory.decodeByteArray(imageBytes, 0, imageBytes.size)
            ?: return "[]"
        // 스크린샷은 회전이 없다 — rotationDegrees=0 고정.
        // (RapidOCR 경로에서 방향 분류기를 끈 것과 같은 불변식이다.)
        val text = Tasks.await(recognizer.process(InputImage.fromBitmap(bmp, 0)))

        val out = JSONArray()
        for (block in text.textBlocks) {
            when (granularity) {
                "block" -> put(out, block.text, block.boundingBox)
                "element" -> for (line in block.lines) for (el in line.elements) put(out, el.text, el.boundingBox)
                else -> for (line in block.lines) put(out, line.text, line.boundingBox)
            }
        }
        return out.toString()
    }

    private fun put(arr: JSONArray, text: String?, box: android.graphics.Rect?) {
        if (text.isNullOrBlank() || box == null) return
        arr.put(JSONObject().apply {
            put("text", text)
            put("x", box.left)
            put("y", box.top)
            put("w", box.width())
            put("h", box.height())
            put("conf", 1.0)   // 위 주석 (1) 참조 — ML Kit은 요소별 신뢰도를 주지 않는다
        })
    }
}
