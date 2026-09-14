// Player-wide progress must outlive the creature that earned it. Badges, the
// avatar, the streak and the Pokedex all belong to the player, not the pet, so
// none of the three endings may take them.
#include "Arduino.h"
#include "Preferences.h"
#include "pet.h"
#include "party.h"
#include <cstdio>
#include <cstring>
uint32_t g_seed=9; FakeSerial Serial; FakeESP ESP; FakeWire Wire;
volatile int g_touchX=0,g_touchY=0; volatile bool g_touchDown=false; bool wasPressed=false;
static uint32_t g_ms=0; uint32_t millis(){return g_ms;}
void FakeESP::restart(){exit(0);}
int FakeSerial::available(){return 0;} String FakeSerial::readStringUntil(char){return String("");}
void sfxPlay(uint8_t){}
static int bad=0;
static void ck(bool ok,const char*w){printf("%s  %s\n",ok?"PASS":"FAIL",w); if(!ok)bad++;}

static void award(Pet &p){
  p.winBadge(0,0,false); p.winBadge(0,1,false); p.winBadge(0,0,true);
  p.winBadge(2,3,false);   // and one in another region's ladder
  p.avatar = 2; p.streak = 9; p.bestStreak = 11; p.totalMedals = 5;
}
static bool intact(Pet &p){
  return p.hasBadge(0,0,false) && p.hasBadge(0,1,false) && p.hasBadge(0,0,true)
      && p.hasBadge(2,3,false)
      && p.avatar==2 && p.streak==9 && p.bestStreak==11 && p.totalMedals==5;
}

int main(){
  Pet p; p.begin();
  if (p.awaitingStarter()) p.chooseStarter(4);
  if (p.isEgg()) p.dbgHatchAs(4,false);
  award(p);
  ck(intact(p), "progress is set");

  // 1. a new egg (what every ending eventually calls)
  p.newEgg();
  ck(intact(p), "survives newEgg()");

  // 2. a reload from NVS
  Pet q; q.begin();
  ck(intact(q), "survives a save/load round trip");

  // 3. each of the three endings, end to end
  const char *names[] = {"farewell","runaway","release"};
  for (int e=0;e<3;e++){
    Pet r; r.begin();
    if (r.isEgg()) r.dbgHatchAs(4,false);
    award(r);
    r.ageMinutes = 4UL*24*60;
    if (e==0) r.startFarewell(); else if (e==1) r.startRunaway(); else r.release();
    g_ms += 60000;                       // let the ceremony expire
    r.update(g_ms);
    char msg[64]; snprintf(msg,sizeof(msg),"survives a %s",names[e]);
    ck(intact(r), msg);
    Pet after; after.begin();
    snprintf(msg,sizeof(msg),"...and is still there after reloading (%s)",names[e]);
    ck(intact(after), msg);
  }
  // --- rival records: keyed by MAC, MRU-ordered, capped at RIVAL_CAP --------
  // Same process, same NVS as everything above -- deliberately continued on
  // `p` rather than a fresh Pet, since a fresh one would load THIS store too
  // (see CLAUDE.md "Tests share one NVS store within a process").
  {
    uint8_t macA[6] = {1,2,3,4,5,6};
    uint8_t macB[6] = {9,9,9,9,9,9};
    uint8_t zero[6] = {0};

    ck(p.findRival(macA) == nullptr, "an unknown rival has no record");
    ck(p.findRival(zero) == nullptr, "an all-zero mac is never a real rival");

    p.recordRivalResult(macA, "ASH", true);
    const RivalRecord *ra = p.findRival(macA);
    ck(ra && ra->wins==1 && ra->losses==0, "a win is recorded against a new rival");
    ck(ra && !strcmp(ra->name,"ASH"), "with the name it was played under");

    p.recordRivalResult(macA, "ASH", false);
    ra = p.findRival(macA);
    ck(ra && ra->wins==1 && ra->losses==1, "a loss adds rather than replaces");

    // Renaming must not reset the score -- the key is the mac, never the name.
    p.recordRivalResult(macA, "MISTY", true);
    ra = p.findRival(macA);
    ck(ra && ra->wins==2 && ra->losses==1, "the score survives a rename");
    ck(ra && !strcmp(ra->name,"MISTY"), "and the shown name follows the rename");

    p.recordRivalResult(macB, "GARY", true);
    ck(!memcmp(p.rivals[0].mac, macB, 6), "the most recently played rival leads the table");
    ck(!memcmp(p.rivals[1].mac, macA, 6), "and the other one sits right behind it");

    p.recordRivalResult(macA, "MISTY", true);
    ck(!memcmp(p.rivals[0].mac, macA, 6), "playing an old rival again moves it back to the front");

    // RIVAL_CAP more DISTINCT rivals: macB falls off the 9th, macA the 10th --
    // negative-checked below rather than assumed.
    uint8_t mac[RIVAL_CAP][6];
    for (int i = 0; i < RIVAL_CAP; i++) {
      for (int b = 0; b < 6; b++) mac[i][b] = (uint8_t)(100 + i);
      char nm[12]; snprintf(nm, sizeof(nm), "R%d", i);
      p.recordRivalResult(mac[i], nm, true);
    }
    ck(p.findRival(macB) == nullptr, "the older of the two originals is evicted first");
    ck(p.findRival(macA) == nullptr, "and the other follows once the table is full of newer ones");
    ck(!memcmp(p.rivals[0].mac, mac[RIVAL_CAP-1], 6), "the newest insert leads the table");
    const RivalRecord *rn = p.findRival(mac[0]);
    ck(rn && rn->wins==1 && rn->losses==0, "and the oldest surviving entry still has its own score");

    Pet after; after.begin();
    ck(after.findRival(macA) == nullptr, "eviction survives a save/load round trip too");
    const RivalRecord *ka = after.findRival(mac[RIVAL_CAP-1]);
    ck(ka && ka->wins==1 && !strcmp(ka->name, "R9"), "and a kept record reloads intact");
  }

  printf("%s\n", bad?"FAILURES":"all good");
  return bad?1:0;
}
