#include "pedometer.h"
#include "pin_config.h"
#include <SensorQMI8658.hpp>
#include <math.h>

// SensorLib (same library the touch driver already depends on -- see the
// TouchDrvCSTXXX.hpp include in TamaPoke.ino). Not every board of this family
// carries the QMI8658; begin() failing is a real, expected outcome, not a bug,
// and every call below is guarded on it so a board without one just never
// earns any steps rather than crashing.
static SensorQMI8658 qmi;
static bool qmiOk = false;

// SOFTWARE step detection, not the chip's onboard pedometer engine.
//
// The engine (configPedometer()/getPedometerCounter()) was tried first and
// abandoned after real-board testing 2026-09-11: verbose ESP32 core logging
// confirmed clean I2C throughout (chip found, every CTRL9 handshake command
// -- including configPedometer()'s two internal calls -- completed with no
// timeout, no error anywhere) and STILL zero steps counted, for 12s of active
// shaking AND for genuine walking. A broadened search for anyone with this
// engine actually working through this library -- not just on this board,
// the chip in general -- turned up nobody, plus an unrelated open issue
// asking whether anyone has gotten the chip's OTHER onboard fusion engine
// (AttitudeEngine) working either, also unanswered. Meanwhile two independent
// community projects on this exact chip (VolosR/stepCounter, a Waveshare
// AMOLED community project; Melaja/ESP32-S3-Smartwatch) both bypass the
// hardware engine entirely for a software magnitude-threshold detector on
// raw accelerometer samples -- which is what this is.
//
// STEP_THRESHOLD_G started at VolosR/stepCounter's own proven 1.8g -- but a
// real-hardware capture of this exact board/grip 2026-09-11 (once the
// getDataReady() bug below was found and fixed) topped out at 1.70g over an
// 8s active-shake burst, never reaching 1.8g. Lowered to 1.5g against that
// real measurement, which CONFIRMED the whole pipeline end to end: wallet
// and stepsTotal both moved by 10 on a real board during real motion.
// Lowered further to 1.35g from there for sensitivity, still against real
// hardware behaviour rather than a borrowed number. STEP_DEBOUNCE_MS is
// still VolosR's own proven value.
static constexpr float STEP_THRESHOLD_G = 1.35f;
static constexpr uint32_t STEP_DEBOUNCE_MS = 100;
static float lastMagnitude = 0.0f;
static uint32_t lastStepMs = 0;

bool pedoBegin() {
  qmiOk = qmi.begin(Wire, QMI8658_L_SLAVE_ADDRESS, IIC_SDA, IIC_SCL);
  if (!qmiOk) {
    Serial.println("QMI8658 not detected -- no pedometer this session");
    return false;
  }
  Serial.println("QMI8658 found -- software step detection (magnitude threshold)");
  // ACC_ODR_LOWPOWER_128Hz: fast enough to resolve a footstep's ~50-150ms
  // impact peak (a threshold-crossing detector needs to actually see the
  // waveform, unlike the abandoned hardware engine which only needed an
  // occasional read of its own internal accumulator) while still being one
  // of the chip's designated low-power modes rather than a "normal" ODR.
  // How much current this specific mode draws is NOT verified -- the
  // datasheet's power tables were not extractable from here -- worth an eye
  // on HEALTH's numbers on real hardware.
  qmi.configAccelerometer(SensorQMI8658::ACC_RANGE_2G,
                          SensorQMI8658::ACC_ODR_LOWPOWER_128Hz);
  qmi.enableAccelerometer();
  return true;
}

// Steps detected since the last call. Meant to be polled often (tens of ms,
// not seconds -- see pedoBegin()'s comment on why) from loop(); at that
// cadence this returns 0 or 1, never more, since two real footsteps cannot
// land inside one poll interval.
uint32_t pedoPollSteps() {
  if (!qmiOk) return 0;
  // NOT gated on getDataReady(): that checks a STATUS0 ready-bit which, in
  // this ODR/power-mode combination, may simply never behave the way the
  // library expects -- if so it would silently return early on literally
  // every poll, which is indistinguishable from "no steps" and was the
  // leading suspect after the first real-hardware test of this approach
  // read completely flat. A threshold detector does not need a guaranteed-
  // fresh sample; at 128Hz internal sampling against a ~33Hz poll rate
  // there is always a recent value sitting in the register regardless.
  float x, y, z;
  if (!qmi.getAccelerometer(x, y, z)) return 0;
  float magnitude = sqrtf(x * x + y * y + z * z);
  uint32_t now = millis();
  uint32_t steps = 0;
  // Rising edge only -- crossing UP through the threshold -- so a footstep
  // sustained above it for several samples counts once, not once per sample.
  // The debounce on top of that stops one footstep's impact-then-settle
  // ripple from re-crossing and counting again before the body has actually
  // taken a second step.
  if (lastMagnitude < STEP_THRESHOLD_G && magnitude >= STEP_THRESHOLD_G &&
      now - lastStepMs >= STEP_DEBOUNCE_MS) {
    steps = 1;
    lastStepMs = now;
  }
  lastMagnitude = magnitude;
  return steps;
}

float pedoLastMagnitude() { return lastMagnitude; }
