package staff.android.seatlight

import android.Manifest
import android.os.Build
import android.os.Bundle
import android.content.pm.PackageManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity: ComponentActivity() {
    private val nearby=registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) ViewModelProvider(this)[SeatViewModel::class.java].refresh()
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState); enableEdgeToEdge()
        if (Build.VERSION.SDK_INT>=37 && checkSelfPermission(Manifest.permission.ACCESS_LOCAL_NETWORK)!=PackageManager.PERMISSION_GRANTED)
            nearby.launch(Manifest.permission.ACCESS_LOCAL_NETWORK)
        val model=ViewModelProvider(this)[SeatViewModel::class.java]
        setContent { SeatTheme { val state by model.state.collectAsStateWithLifecycle(); Seatlight(state,model) } }
    }
}
private val BodyFont=FontFamily(Font(R.font.google_sans_regular),Font(R.font.google_sans_bold,FontWeight.Bold))
private val TitleFont=FontFamily(Font(R.font.sora_bold,FontWeight.Bold))
@Composable private fun SeatTheme(content:@Composable ()->Unit) {
    val dark=isSystemInDarkTheme()
    val colors=if(dark) darkColorScheme(
        primary=Color(0xFFFFB595),onPrimary=Color(0xFF3B1D12),background=Color(0xFF201A17),onBackground=Color(0xFFF8EDE2),
        surface=Color(0xFF2D2420),onSurface=Color(0xFFF8EDE2),
        surfaceVariant=Color(0xFF41342B),onSurfaceVariant=Color(0xFFEDDDCF),
        secondaryContainer=Color(0xFF563E30),onSecondaryContainer=Color(0xFFF5DDCB),
        outline=Color(0xFFAF9787),outlineVariant=Color(0xFF655145))
        else lightColorScheme(
        primary=Color(0xFF98462C),onPrimary=Color(0xFFFFFCF8),background=Color(0xFFFCF8F1),onBackground=Color(0xFF25211F),
        surface=Color(0xFFFFFCF8),onSurface=Color(0xFF25211F),
        surfaceVariant=Color(0xFFF0E7DC),onSurfaceVariant=Color(0xFF51453C),
        secondaryContainer=Color(0xFFEBD7C5),onSecondaryContainer=Color(0xFF4C3025),
        outline=Color(0xFF806F63),outlineVariant=Color(0xFFD6C6B8))
    val type=Typography().let { it.copy(bodyLarge=it.bodyLarge.copy(fontFamily=BodyFont),bodyMedium=it.bodyMedium.copy(fontFamily=BodyFont),titleMedium=it.titleMedium.copy(fontFamily=BodyFont),labelLarge=it.labelLarge.copy(fontFamily=BodyFont)) }
    MaterialTheme(colorScheme=colors,typography=type,content=content)
}
@Composable private fun Seatlight(state:ScreenState, model:SeatViewModel) {
    var bookingTab by rememberSaveable { mutableStateOf(false) }
    var about by remember { mutableStateOf(false) }
    LaunchedEffect(state.booking?.id) { if(state.booking!=null) bookingTab=true }
    Scaffold(containerColor=MaterialTheme.colorScheme.background,topBar={
        Column(Modifier.statusBarsPadding().padding(horizontal=20.dp)) {
            Row(Modifier.fillMaxWidth().padding(top=12.dp,bottom=8.dp),verticalAlignment=Alignment.CenterVertically) {
                Text("seatlight",fontFamily=TitleFont,fontSize=25.sp,modifier=Modifier.weight(1f))
                TextButton(onClick={about=true},modifier=Modifier.testTag("about")){Text("About")}
            }
            Row(horizontalArrangement=Arrangement.spacedBy(12.dp)) {
                FilterChip(selected=!bookingTab,onClick={bookingTab=false},label={Text("Workshops")},modifier=Modifier.testTag("tab-workshops"))
                FilterChip(selected=bookingTab,onClick={bookingTab=true},label={Text("My booking")},modifier=Modifier.testTag("tab-booking"))
            }
        }
    }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),verticalArrangement=Arrangement.spacedBy(20.dp)) {
            if(state.busy) LinearProgressIndicator(Modifier.fillMaxWidth().testTag("busy"))
            if(!bookingTab) {
                Text("MAKE TIME\nFOR SOMETHING REAL.",fontFamily=TitleFont,fontSize=31.sp,lineHeight=37.sp)
                Pottery(Modifier.fillMaxWidth().height(220.dp))
                FlowRow(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.spacedBy(20.dp),verticalArrangement=Arrangement.spacedBy(8.dp)) {
                    Text("HANDS-ON · 90 MIN",style=MaterialTheme.typography.labelLarge,color=MaterialTheme.colorScheme.primary)
                    Text("₹1,499",fontWeight=FontWeight.Bold,fontSize=21.sp)
                }
                Text("Clay workshop",fontFamily=TitleFont,fontSize=28.sp)
                Text("Slow down, get your hands into clay, and shape a small bowl to take home. Materials and studio time are included.",style=MaterialTheme.typography.bodyLarge)
                Text("North studio  /  Small-group session",style=MaterialTheme.typography.bodyMedium)
                Surface(shape=RoundedCornerShape(16.dp),color=MaterialTheme.colorScheme.surfaceVariant) {
                    Column(Modifier.padding(16.dp),verticalArrangement=Arrangement.spacedBy(6.dp)) {
                        Text(when(state.available){null->"Availability not yet checked";0->"No unheld seats right now";1->"One seat available";else->"${state.available} seats available"},fontWeight=FontWeight.Bold)
                        Text("Hold one seat for 10 minutes. Your booking is confirmed only when the service verifies payment.")
                    }
                }
                if(state.intent!=null) Button(onClick={bookingTab=true},modifier=Modifier.fillMaxWidth()){Text("View your booking")}
                else Button(onClick=model::hold,enabled=!state.busy && !state.storageFailure && (state.available ?: 0)>0,modifier=Modifier.fillMaxWidth().testTag("hold")){Text("Hold a seat")}
                TextButton(onClick=model::refresh,enabled=!state.busy,modifier=Modifier.testTag("refresh")){Text("Refresh availability")}
            } else {
                val booking=state.booking
                Text("Your studio time",fontFamily=TitleFont,fontSize=30.sp)
                if(booking==null) {
                    Text(if(state.intent==null) "Your next experience starts with a seat." else "Your request reference is saved. Check the service before starting another booking.")
                    if(state.intent!=null) Button(onClick=model::refresh,enabled=!state.busy,modifier=Modifier.fillMaxWidth().testTag("retry")){Text("Retry saved request")}
                    else OutlinedButton(onClick={bookingTab=false},modifier=Modifier.fillMaxWidth()){Text("Explore workshops")}
                } else {
                    if(state.intent?.action?.isNotEmpty()==true) Text("Previously observed booking. Delivery of your saved request is unresolved; retry that request before choosing another action.")
                    BookingCard(booking,state.busy || state.intent?.action?.isNotEmpty()==true,model)
                }
            }
            state.availabilityProblem?.let { Text(it,color=MaterialTheme.colorScheme.onSurfaceVariant) }
            state.problem?.let { message ->
                Surface(color=MaterialTheme.colorScheme.errorContainer,shape=RoundedCornerShape(16.dp)) {
                    Column(Modifier.padding(16.dp),verticalArrangement=Arrangement.spacedBy(8.dp)) {
                        Text(message,color=MaterialTheme.colorScheme.onErrorContainer,modifier=Modifier.testTag("problem"))
                        if(!state.storageFailure) TextButton(onClick=model::refresh,enabled=!state.busy){Text("Retry same request")}
                    }
                }
            }
            Text("A learning experience · All payments are simulated. No card details or money are collected.",style=MaterialTheme.typography.bodyMedium,color=MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(12.dp))
        }
    }
    if(about) AlertDialog(onDismissRequest={about=false},title={Text("About Seatlight")},text={Text("A local companion to Chapter 33 of The Staff Android Engineer. The booking service and payment simulator keep separate records. Fault controls live in the companion terminal guide, outside the booking journey.")},confirmButton={TextButton(onClick={about=false}){Text("Close")}})
}

@Composable private fun BookingCard(b:Booking,busy:Boolean,model:SeatViewModel) {
    val locale=androidx.compose.ui.platform.LocalLocale.current.platformLocale
    val title=when(b.state){"HELD"->"A seat is on hold";"CONFIRMED"->"You're booked in";"PAYMENT_PENDING"->"Checking your payment";"CANCEL_PENDING"->"Cancellation requested";"REFUND_PENDING"->"Refund in progress";"CANCELLED"->"Booking cancelled";"EXPIRED"->"Your hold expired";else->"Payment was declined"}
    Surface(shape=RoundedCornerShape(24.dp),color=MaterialTheme.colorScheme.surface,modifier=Modifier.border(1.dp,MaterialTheme.colorScheme.outlineVariant,RoundedCornerShape(24.dp))) {
        Column(Modifier.padding(22.dp),verticalArrangement=Arrangement.spacedBy(14.dp)) {
            Text("CLAY WORKSHOP",color=MaterialTheme.colorScheme.primary,style=MaterialTheme.typography.labelLarge)
            Text(title,fontFamily=TitleFont,fontSize=26.sp,modifier=Modifier.testTag("state-${b.state}"))
            Text("North studio · One seat · 90 minutes")
            HorizontalDivider()
            Text("₹1,499",fontSize=28.sp,fontWeight=FontWeight.Bold)
            Text(when(b.state){
                "HELD"->"Held until ${SimpleDateFormat("HH:mm:ss",locale).format(Date(b.expires*1000))}, according to the service. The service checks expiry again when you pay."
                "PAYMENT_PENDING"->"The payment outcome is not established yet. Your seat stays allocated while we check the same payment reference."
                "CANCEL_PENDING"->"We must establish the payment outcome before completing cancellation. If payment was captured, it will be refunded."
                "REFUND_PENDING"->"Payment was captured. We are waiting for the matching refund receipt; cancellation is not complete yet."
                "CONFIRMED"->"The service verified the captured payment and confirmed this booking."
                "CANCELLED"->if(b.refund.isNotEmpty()) "A full simulated refund is verified. The seat has been released." else "No captured payment remains to settle. The seat has been released."
                "EXPIRED"->"Payment did not start before the hold expired. No payment was requested for this booking."
                else->"The provider recorded a decline. The seat has been released. This booking will not retry the charge."
            },style=MaterialTheme.typography.bodyLarge)
            Text("Reference ${b.id.take(8)}",style=MaterialTheme.typography.bodyMedium)
        }
    }
    when(b.state) {
        "HELD"->{
            Button(onClick=model::pay,enabled=!busy,modifier=Modifier.fillMaxWidth().testTag("pay")){Text("Pay ₹1,499 · simulated")}
            OutlinedButton(onClick=model::cancel,enabled=!busy,modifier=Modifier.fillMaxWidth().testTag("cancel")){Text("Release this hold")}
        }
        "CONFIRMED"->OutlinedButton(onClick=model::cancel,enabled=!busy,modifier=Modifier.fillMaxWidth().testTag("cancel")){Text("Cancel and refund")}
        "PAYMENT_PENDING","CANCEL_PENDING","REFUND_PENDING"->{
            Button(onClick=model::reconcile,enabled=!busy,modifier=Modifier.fillMaxWidth().testTag("check")){Text("Check outcome")}
            if(b.state=="PAYMENT_PENDING") OutlinedButton(onClick=model::cancel,enabled=!busy,modifier=Modifier.fillMaxWidth().testTag("cancel")){Text("Request cancellation")}
        }
        else->Button(onClick=model::newBooking,enabled=!busy,modifier=Modifier.fillMaxWidth().testTag("new-booking")){Text("Explore another booking")}
    }
    TextButton(onClick=model::refresh,enabled=!busy,modifier=Modifier.testTag("booking-refresh")){Text("Refresh booking")}
}

/** Original editable pottery geometry: no downloaded campaign or retailer assets. */
@Composable private fun Pottery(modifier:Modifier) {
    Canvas(modifier.clip(RoundedCornerShape(28.dp)).background(Color(0xFFEACDB5))) {
        val w=size.width;val h=size.height
        drawCircle(Color(0xFFF7E2CA),h*.59f,Offset(w*.77f,h*.10f))
        drawOval(Color(0xFFBD9479),Offset(w*.12f,h*.77f),Size(w*.76f,h*.15f))
        val bowl=Path().apply { moveTo(w*.18f,h*.33f); cubicTo(w*.20f,h*.86f,w*.76f,h*.96f,w*.83f,h*.33f);close() }
        drawPath(bowl,Color(0xFFA6583C))
        for(i in 0..4) drawArc(Color(0xFFC67F5F),0f,180f,false,Offset(w*(.185f+i*.01f),h*(.35f+i*.07f)),Size(w*(.64f-i*.018f),h*.19f),style=Stroke(2.dp.toPx()))
        drawOval(Color(0xFFCF8E67),Offset(w*.18f,h*.24f),Size(w*.65f,h*.22f))
        drawOval(Color(0xFF79452F),Offset(w*.215f,h*.28f),Size(w*.58f,h*.13f))
        drawOval(Color(0xFFAE6845),Offset(w*.24f,h*.32f),Size(w*.53f,h*.08f))
    }
}
