package staff.android.seatlight

import android.Manifest
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.rule.GrantPermissionRule
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.Assert.assertEquals
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Fresh app and fresh fixture ledgers. Uses actual Android UI and both HTTP services. */
@RunWith(AndroidJUnit4::class)
class BookingJourneyTest {
    @get:Rule(order=0) val permission=GrantPermissionRule.grant(Manifest.permission.ACCESS_LOCAL_NETWORK)
    @get:Rule(order=1) val compose=createAndroidComposeRule<MainActivity>()
    @Test fun lostCaptureAndRefundRepliesDoNotDuplicateEffects() {
        try {
            waitFor("hold");tap("hold");waitFor("state-HELD")
            control(8901,"{\"drop_capture_reply\":true}")
            tap("pay");waitFor("state-PAYMENT_PENDING")
            assertEquals(1, control(8901).getJSONArray("captures").length())
            tap("check");waitFor("state-CONFIRMED")
            assertEquals(1, control(8901).getJSONArray("captures").length())
            control(8901,"{\"drop_refund_reply\":true}")
            tap("cancel");waitFor("state-REFUND_PENDING")
            assertEquals(1, control(8901).getJSONArray("refunds").length())
            tap("check");waitFor("state-CANCELLED")
            assertEquals(1, control(8901).getJSONArray("refunds").length())
            tap("new-booking");tap("tab-workshops");waitFor("hold");tap("hold");waitFor("state-HELD")
            control(8900,"{\"advance_seconds\":600}")
            tap("booking-refresh");waitFor("state-EXPIRED")
            assertEquals(1, control(8901).getJSONArray("captures").length())
        } finally { control(8900,"{}");control(8901,"{}") }
    }
    private fun waitFor(tag:String)=compose.waitUntil(20_000) { compose.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty() }
    private fun tap(tag:String) {
        compose.waitUntil(20_000) { compose.onAllNodesWithTag("busy").fetchSemanticsNodes().isEmpty() }
        val node=compose.onNodeWithTag(tag)
        if(!tag.startsWith("tab-")) node.performScrollTo()
        node.assertIsEnabled().performClick()
    }
    private fun control(port:Int,body:String?=null):JSONObject {
        val connection=URL("http://10.0.2.2:$port/__control").openConnection() as HttpURLConnection
        try {
            connection.connectTimeout=5000;connection.readTimeout=5000
            if(body!=null) { connection.requestMethod="POST";connection.doOutput=true;connection.setRequestProperty("Content-Type","application/json");connection.outputStream.use { it.write(body.toByteArray()) } }
            check(connection.responseCode==200)
            return JSONObject(connection.inputStream.use { it.bufferedReader().readText() })
        } finally { connection.disconnect() }
    }
}
