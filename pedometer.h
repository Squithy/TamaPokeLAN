#pragma once
#include <Arduino.h>

// The QMI8658 6-axis IMU on the board's shared I2C bus (same SDA/SCL as the
// touch controller). Only the accelerometer is used, read as raw samples for
// a SOFTWARE magnitude-threshold step detector -- the chip's own onboard
// pedometer engine does not work through this library; see pedometer.cpp's
// header comment and HANDOVER.md's dated entry for the full story. Real
// board implementation is pedometer.cpp; the emulator has no IMU and stubs
// both calls in host_impl.cpp, the same way rtcbat.cpp/audio.cpp/linknow.cpp
// are stubbed there instead of being compiled by tools/emu/build.sh.
bool pedoBegin();

// Steps detected since the last call (0 or 1 at the polling cadence loop()
// actually uses -- see its call site). A poll before the chip was ever found
// is just 0.
uint32_t pedoPollSteps();

// The most recent accelerometer magnitude (g), updated on every pedoPollSteps()
// call regardless of whether it crossed the step threshold. Diagnostic only --
// lets the serial console show live sensor values without a second I2C read.
float pedoLastMagnitude();
