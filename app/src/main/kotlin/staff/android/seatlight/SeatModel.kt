package staff.android.seatlight

import android.app.Application
import android.util.AtomicFile
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.MediaType.Companion.toMediaType
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit

data class Booking(val id: String, val state: String, val amount: Long,
    val expires: Long, val observedAt: Long, val capture: String, val refund: String)
data class IntentRecord(val id: String, val action: String)
data class ScreenState(val booking: Booking? = null, val available: Int? = null,
    val busy: Boolean = true, val problem: String? = null, val intent: IntentRecord? = null,
    val storageFailure: Boolean = false, val availabilityProblem: String? = null)

/** Bounded local simulator transport. No automatic connection retry of commands. */
class SeatApi {
    private val client = OkHttpClient.Builder().retryOnConnectionFailure(false)
        .followRedirects(false).callTimeout(5, TimeUnit.SECONDS).build()
    fun request(path: String, body: JSONObject? = null): JSONObject {
        val builder = Request.Builder().url("http://10.0.2.2:8900$path")
        if (body != null) builder.post(body.toString().toRequestBody("application/json".toMediaType()))
        client.newCall(builder.build()).execute().use { response ->
            val out = java.io.ByteArrayOutputStream()
            response.body.byteStream().use { input ->
                val buffer = ByteArray(2048)
                while (true) {
                    val n = input.read(buffer)
                    if (n == -1) break
                    if (n > 16_384 - out.size()) throw IOException("Oversized response")
                    out.write(buffer, 0, n)
                }
            }
            val decoder = Charsets.UTF_8.newDecoder()
                .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
            val json = JSONObject(decoder.decode(java.nio.ByteBuffer.wrap(out.toByteArray())).toString())
            if (!response.isSuccessful) throw ApiProblem(response.code, json.optString("error"), json.optString("code"))
            return json
        }
    }
}
class ApiProblem(val code: Int, message: String, val reason: String): IOException(message)

class SeatViewModel(application: Application): AndroidViewModel(application) {
    private val file = AtomicFile(File(application.filesDir, "booking-intent.json"))
    private val api = SeatApi()
    private var generation = 0L
    private val mutable = MutableStateFlow(ScreenState())
    val state = mutable.asStateFlow()
    init { restore() }

    private fun save(record: IntentRecord) {
        val output = file.startWrite()
        try {
            output.write(JSONObject().put("id", record.id).put("action",record.action).toString().toByteArray())
            file.finishWrite(output)
        } catch (error: Exception) { file.failWrite(output); throw error }
    }
    private fun read(): IntentRecord? {
        if (!file.baseFile.exists() && !File(file.baseFile.path+".bak").exists()) return null
        val json = JSONObject(String(file.readFully(), Charsets.UTF_8))
        val id = json.getString("id"); val action = json.getString("action")
        require(UUID.fromString(id).toString() == id)
        require(action in setOf("create", "pay", "cancel", ""))
        return IntentRecord(id, action)
    }
    private fun restore() = viewModelScope.launch {
        val intent = try { withContext(Dispatchers.IO) { read() } }
        catch (error: Exception) {
            if (error is CancellationException) throw error
            mutable.value = ScreenState(busy=false, problem="The saved booking record could not be read. It has been preserved for recovery.", storageFailure=true)
            return@launch
        }
        mutable.value = mutable.value.copy(intent=intent)
        load(intent)
    }
    fun refresh() {
        if (state.value.busy || state.value.storageFailure) return
        generation++
        mutable.value = state.value.copy(busy=true,problem=null)
        viewModelScope.launch { load(state.value.intent) }
    }
    fun hold() = command("create")
    fun pay() = command("pay")
    fun cancel() = command("cancel")
    fun reconcile() = command("reconcile")
    private fun command(action: String) {
        val before=state.value
        if (before.busy || before.storageFailure) return
        if (before.intent?.action?.isNotEmpty() == true && action != before.intent.action && action != "reconcile") return
        val record = when {
            before.intent?.action?.isNotEmpty() == true -> before.intent
            action == "create" -> IntentRecord(UUID.randomUUID().toString(), "create")
            before.intent != null -> before.intent.copy(action=if(action=="reconcile") "" else action)
            else -> return
        }
        generation++
        mutable.value = before.copy(busy=true,problem=null)
        viewModelScope.launch {
            try {
                // Persist before sending. A failed durable write admits no request.
                withContext(Dispatchers.IO) { save(record) }
                mutable.value = state.value.copy(intent=record)
                load(record, reconcile=action=="reconcile")
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                mutable.value = before.copy(busy=false,problem="Could not save this request. Nothing new was sent.")
            }
        }
    }
    private suspend fun load(intent: IntentRecord?, reconcile: Boolean = false) {
        val token = ++generation
        if (intent != null) {
            try {
                val booking = withContext(Dispatchers.IO) {
                    val json = when {
                        intent.action == "create" -> api.request("/bookings", JSONObject().put("bookingId",intent.id).put("workshopId","clay-01"))
                        intent.action.isNotEmpty() -> api.request("/bookings/${intent.id}/${intent.action}",JSONObject())
                        reconcile -> api.request("/bookings/${intent.id}/reconcile",JSONObject())
                        else -> api.request("/bookings/${intent.id}")
                    }
                    decode(json, intent.id).also { save(intent.copy(action="")) }
                }
                if (token != generation) return
                // A known booking outcome is useful before optional workshop availability.
                mutable.value = state.value.copy(booking=booking, intent=intent.copy(action=""), busy=false,problem=null)
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                if (token != generation) return
                if (error is ApiProblem && error.code==409 && error.reason=="hold_rejected" && intent.action=="create") {
                    withContext(Dispatchers.IO) { file.delete() }
                    mutable.value = state.value.copy(booking=null,intent=null,busy=false,
                        problem="The service rejected this hold request. No seat was reserved. Refresh availability before trying again.")
                } else {
                    val message = if(error is ApiProblem && error.code==404)
                        "The service cannot find this booking. Keep its reference and investigate; this is not proof of cancellation."
                    else "We could not establish the latest outcome. Your request reference is saved. Retry the same request when connected."
                    mutable.value = state.value.copy(busy=false,problem=message)
                    return
                }
            }
        }
        try {
            val available = withContext(Dispatchers.IO) {
                val workshop=api.request("/workshop")
                require(workshop.get("workshopId")=="clay-01")
                val capacity=integer(workshop,"capacity")
                val free=integer(workshop,"available")
                require(capacity in 1L..100L && free in 0L..capacity)
                free.toInt()
            }
            if(token==generation) mutable.value=state.value.copy(available=available,busy=false,availabilityProblem=null)
        } catch(error:Exception) {
            if(error is CancellationException) throw error
            if(token==generation) mutable.value=state.value.copy(busy=false,
                availabilityProblem="Workshop availability could not be refreshed. The booking result above is unchanged.")
        }
    }
    fun newBooking() {
        if (state.value.busy || state.value.booking?.state !in setOf("CANCELLED","EXPIRED","PAYMENT_FAILED")) return
        generation++
        mutable.value = state.value.copy(busy=true)
        viewModelScope.launch {
            withContext(Dispatchers.IO) { file.delete() }
            mutable.value = ScreenState(busy=true)
            load(null)
        }
    }
    private fun integer(json: JSONObject, key: String): Long {
        val value=json.get(key);require(value is Int || value is Long)
        return (value as Number).toLong()
    }
    private fun decode(json: JSONObject, id: String): Booking {
        require(json.get("bookingId")==id && json.get("workshopId")=="clay-01")
        require(json.get("currency")=="INR" && integer(json,"priceMinor")==149900L)
        val phase=json.get("state");require(phase is String && phase in setOf("HELD","PAYMENT_PENDING","CANCEL_PENDING","CONFIRMED","REFUND_PENDING","CANCELLED","EXPIRED","PAYMENT_FAILED"))
        val capture=json.get("captureId");val refund=json.get("refundId");require(capture is String && refund is String)
        require(phase !in setOf("CONFIRMED","REFUND_PENDING") || capture.isNotEmpty())
        require(refund.isEmpty() || (capture.isNotEmpty() && phase=="CANCELLED"))
        return Booking(id,phase,integer(json,"priceMinor"),integer(json,"expiresAt"),integer(json,"serverNow"),capture,refund)
    }
}
