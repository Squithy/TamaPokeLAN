// Host stub for nvsinfo.h's nvsEntryStats() -- the emulator's NVS is a
// std::map in Preferences.h, so there is no real partition to report on and
// this returns a healthy, fixed figure rather than modelling page accounting
// it does not have.
//
// In its OWN file, separate from host_impl.cpp, because pet.cpp calls this
// directly now (see logCkptFailure() in pet.cpp, added alongside the
// checkpoint-failure diagnostics): any test binary that links pet.cpp needs
// SOME definition of it, including the plain CORE-only unit tests that never
// pull in the rest of host_impl.cpp's hardware stubs at all. Splitting it out
// lets both link together in the tests that DO need the full stub set,
// without a duplicate-symbol error.
#include "Arduino.h"

bool nvsEntryStats(uint32_t *used, uint32_t *available, uint32_t *total) {
  if (used) *used = 0;
  if (available) *available = 630;   // ~5 pages of a stock 20 KB nvs
  if (total) *total = 630;
  return true;
}

// Same reasoning as nvsEntryStats() above: the emulator's Preferences is a
// std::map with no real esp_err_t underneath it to report, and no put*() call
// there ever actually fails -- so these two are never exercised by a test on
// their real (board-only) behavior, only linked so pet.cpp/party.cpp/
// inventory.cpp resolve.
const char *nvsProbeWrite() { return "ESP_OK"; }
const char *nvsProbeKey(const char *) { return "ESP_OK"; }
const char *nvsProbeBlobWrite(const char *, const void *, size_t) { return "ESP_OK"; }
void logKeyFailure(const char *context, const char *key) {
  Serial.printf("save: %s key '%s' failed (emulator -- should not happen)\n",
                context, key);
}
