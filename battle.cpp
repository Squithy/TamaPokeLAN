#include "battle.h"
#include "dex.h"
#include "types.h"

// ---------- building a combatant ----------

static void fill(Combatant &c, int16_t dex, uint8_t lvl, uint16_t hp,
                 uint16_t a, uint16_t d, uint16_t sa, uint16_t sd, uint16_t sp) {
  c = Combatant();
  c.dex = dex;
  c.level = lvl;
  c.maxHp = hp ? hp : 1;
  c.hp = c.maxHp;
  c.base[SI_ATK] = a; c.base[SI_DEF] = d;
  c.base[SI_SPA] = sa; c.base[SI_SPD] = sd; c.base[SI_SPE] = sp;
}

void combatantFromPet(Combatant &c, const Pet &p) {
  fill(c, p.speciesId, p.level(), p.vitStat(), p.atkStat(), p.defStat(),
       p.spaStat(), p.spdStat(), p.speStat());
  for (int i = 0; i < MOVE_SLOTS; i++) c.moves[i] = p.moves[i];
  c.shiny = p.shiny;
  const char *nm = p.nick[0] ? p.nick : DEX_TBL[p.speciesId].name;
  snprintf(c.name, sizeof(c.name), "%s", nm);
}

void combatantFromParty(Combatant &c, const PartyMon &m) {
  fill(c, m.dex, (uint8_t)m.level, party.vitOf(m), party.atkOf(m), party.defOf(m),
       party.spaOf(m), party.spdOf(m), party.speOf(m));
  for (int i = 0; i < MOVE_SLOTS; i++) c.moves[i] = m.moves[i];
  c.shiny = m.shiny != 0;
  const char *nm = m.nick[0] ? m.nick : DEX_TBL[m.dex].name;
  snprintf(c.name, sizeof(c.name), "%s", nm);
}

// ---------- stat stages ----------

// The series' own table, as a fraction so it stays integer: +1 is 3/2, -1 is
// 2/3, and so on out to +6 = 4x and -6 = 1/4.
uint16_t stagedStat(uint16_t base, int8_t stage) {
  if (stage > 6) stage = 6;
  if (stage < -6) stage = -6;
  uint16_t num = 2 + (stage > 0 ? stage : 0);
  uint16_t den = 2 + (stage < 0 ? -stage : 0);
  uint32_t v = (uint32_t)base * num / den;
  return v < 1 ? 1 : (v > 65535 ? 65535 : (uint16_t)v);
}

static uint16_t effStat(const Combatant &c, uint8_t idx) {
  uint16_t v = stagedStat(c.base[idx], c.stage[idx]);
  // burn halves physical attack, paralysis halves speed
  if (idx == SI_ATK && c.ailment == AIL_BURN) v = v / 2 ? v / 2 : 1;
  if (idx == SI_SPE && c.ailment == AIL_PARA) v = v / 2 ? v / 2 : 1;
  return v;
}

// ---------- damage ----------

// roll is 217..255, the series' damage spread, passed in so tests can pin it.
uint16_t battleDamage(const Combatant &atk, const Combatant &def, uint8_t mv,
                      bool crit, uint8_t roll) {
  if (!mv || mv >= MOVE_COUNT) return 0;
  const MoveEntry &m = MOVE_TBL[mv];
  if (m.cat == MC_STATUS) return 0;

  if (m.effect == EF_FIXED_LVL) return atk.level ? atk.level : 1;
  if (m.effect == EF_FIXED) return m.param > 0 ? (uint16_t)m.param : 1;

  uint16_t A, D;

  if (crit) {
    uint8_t aIdx = (m.cat == MC_PHYS) ? SI_ATK : SI_SPA;
    uint8_t dIdx = (m.cat == MC_PHYS) ? SI_DEF : SI_SPD;

    // Crits ignore attacker's negative stages and defender's positive stages,
    // while keeping buffs on the attacker and debuffs on the defender.
    int8_t aStage = atk.stage[aIdx] > 0 ? atk.stage[aIdx] : 0;
    A = stagedStat(atk.base[aIdx], aStage);
    if (aIdx == SI_ATK && atk.ailment == AIL_BURN) A = A / 2 ? A / 2 : 1;

    int8_t dStage = def.stage[dIdx] < 0 ? def.stage[dIdx] : 0;
    D = stagedStat(def.base[dIdx], dStage);
  } else {
    A = (m.cat == MC_PHYS) ? effStat(atk, SI_ATK) : effStat(atk, SI_SPA);
    D = (m.cat == MC_PHYS) ? effStat(def, SI_DEF) : effStat(def, SI_SPD);
  }

  if (!D) D = 1;

  uint32_t dmg = (2UL * atk.level / 5 + 2) * m.power * A / D / 50 + 2;
  if (crit) dmg = dmg * 3 / 2; // Generation 6+ standard 1.5x (or keep dmg *= 2 for Gen 1-5 legacy)
  if (hasStab(atk.dex, m.type)) dmg = dmg * 3 / 2;

  uint16_t eff = typeEffVsDex(m.type, def.dex);
  dmg = dmg * eff / 100;
  if (eff == 0) return 0;               // immune: no chip, no minimum
  dmg = dmg * roll / 255;

  return dmg < 1 ? 1 : (dmg > 65535 ? 65535 : (uint16_t)dmg);
}

// ---------- turn order ----------

bool battleMovesFirst(const Combatant &a, uint8_t ma,
                      const Combatant &b, uint8_t mb) {
  int pa = (ma && ma < MOVE_COUNT && MOVE_TBL[ma].effect == EF_PRIORITY)
               ? MOVE_TBL[ma].param : 0;
  int pb = (mb && mb < MOVE_COUNT && MOVE_TBL[mb].effect == EF_PRIORITY)
               ? MOVE_TBL[mb].param : 0;
  if (pa != pb) return pa > pb;

  uint16_t sa = effStat(a, SI_SPE), sb = effStat(b, SI_SPE);
  if (sa != sb) return sa > sb;
  return random(2) == 0;                // a genuine speed tie is a coin flip
}

// ---------- one action ----------

static void applyStages(Combatant &c, uint8_t mask, int8_t delta) {
  static const uint8_t BIT[SI_COUNT] = { ST_ATK, ST_DEF, ST_SPA, ST_SPD, ST_SPE };
  for (int i = 0; i < SI_COUNT; i++) {
    if (!(mask & BIT[i])) continue;
    int v = c.stage[i] + delta;
    c.stage[i] = v > 6 ? 6 : (v < -6 ? -6 : (int8_t)v);
  }
}

static void hurt(Combatant &c, uint16_t amount) {
  c.hp = (amount >= c.hp) ? 0 : c.hp - amount;
}

static void heal(Combatant &c, uint16_t amount) {
  uint32_t v = (uint32_t)c.hp + amount;
  c.hp = v > c.maxHp ? c.maxHp : (uint16_t)v;
}

void battleAct(Combatant &atk, Combatant &def, uint8_t mv, TurnLog &log) {
  log = TurnLog();
  log.move = mv;
  if (atk.fainted() || def.fainted()) { log.skipped = true; return; }

  // --- conditions that cost the turn before attack execution
  if (atk.recharge) { atk.recharge = false; log.skipped = true; return; }
  if (atk.ailment == AIL_FREEZE) {
    if (random(100) < 20) atk.ailment = AIL_NONE;   // thaws
    else { log.skipped = true; return; }
  }
  if (atk.ailment == AIL_SLEEP) {
    if (atk.ailTurns) atk.ailTurns--;
    if (atk.ailTurns == 0) atk.ailment = AIL_NONE;
    else { log.skipped = true; return; }
  }
  if (atk.ailment == AIL_PARA && random(100) < 25) { log.skipped = true; return; }
  if (atk.confuseTurns) {
    atk.confuseTurns--;
    if (random(100) < 33) {               // hits itself in confusion
      uint16_t self = (2UL * atk.level / 5 + 2) * 40 *
                          atk.base[SI_ATK] / (atk.base[SI_DEF] ? atk.base[SI_DEF] : 1) / 50 + 2;
      hurt(atk, self);
      log.hurtSelf = true;
      log.damage = self;
      return;
    }
  }

  // charge-up moves (e.g., Solar Beam)
  if (atk.charging) { mv = atk.charging; atk.charging = 0; }
  else if (mv && mv < MOVE_COUNT && MOVE_TBL[mv].effect == EF_CHARGE) {
    atk.charging = mv;
    log.charged = true;
    return;
  }

  if (!mv || mv >= MOVE_COUNT) { log.skipped = true; return; }
  const MoveEntry &m = MOVE_TBL[mv];
  log.move = mv;

  // accuracy check
  if (m.acc && m.effect != EF_NEVER_MISS && random(100) >= m.acc) {
    log.missed = true;
    return;
  }

  if (m.cat == MC_STATUS) {
    if (m.effect == EF_HEAL) {
      heal(atk, (uint32_t)atk.maxHp * (m.param > 0 ? m.param : 50) / 100);
      log.healed = true;
    } else if (m.effect == EF_STAGE) {
      Combatant &t = (m.target == TG_SELF) ? atk : def;
      applyStages(t, m.statMask, m.stages);
      log.stageMask = m.statMask;
      log.stageDelta = m.stages;
    }
    return;
  }

  // damage calculation
  uint8_t hits = (m.effect == EF_MULTI) ? (uint8_t)(2 + random(4)) : 1;
  uint32_t total = 0;
  log.effPct = typeEffVsDex(m.type, def.dex);
  if (log.effPct == 0) { log.immune = true; return; }

  for (uint8_t h = 0; h < hits; h++) {
    bool crit = random(16) == 0;
    uint16_t d = battleDamage(atk, def, mv, crit, (uint8_t)(217 + random(39)));
    total += d;
    if (crit) log.crit = true;
    hurt(def, d);
    if (def.fainted()) { hits = h + 1; break; }
  }

  log.hits = hits;
  log.damage = total > 65535 ? 65535 : (uint16_t)total;

  // secondary effects
  if (m.effect == EF_RECOIL && m.param > 0 && total > 0) {
    uint32_t rec = total / m.param;
    hurt(atk, rec ? (uint16_t)rec : 1);
  }
  if (m.effect == EF_DRAIN && m.param > 0 && total > 0) {
    heal(atk, (uint16_t)(total * m.param / 100));
  }
  if (m.effect == EF_RECHARGE) atk.recharge = true;

  // status conditions
  if (m.ailment != AIL_NONE && m.ailChance && !def.fainted() &&
      random(100) < m.ailChance) {
    if (m.ailment == AIL_CONFUSE) {
      if (!def.confuseTurns) {
        def.confuseTurns = 2 + random(3);
        log.inflicted = AIL_CONFUSE;
      }
    } else if (def.ailment == AIL_NONE) {
      bool immune = (m.ailment == AIL_BURN && hasStab(def.dex, T_FIRE)) ||
                    (m.ailment == AIL_FREEZE && hasStab(def.dex, T_ICE)) ||
                    (m.ailment == AIL_POISON && (hasStab(def.dex, T_POISON) || hasStab(def.dex, T_STEEL))) ||
                    (m.ailment == AIL_PARA && hasStab(def.dex, T_ELECTRIC));
      if (!immune) {
        def.ailment = m.ailment;
        if (m.ailment == AIL_SLEEP) def.ailTurns = 2 + random(3);
        log.inflicted = m.ailment;
      }
    }
  }
  log.targetFainted = def.fainted();
}

// ---------- end of turn ----------

void battleEndTurn(Combatant &c, TurnLog &log) {
  log = TurnLog();
  if (c.fainted()) return;
  if (c.ailment == AIL_BURN || c.ailment == AIL_POISON) {
    uint16_t chip = c.maxHp / 16;
    if (!chip) chip = 1;
    hurt(c, chip);
    log.damage = chip;
    log.inflicted = c.ailment;
    log.targetFainted = c.fainted();
  }
}

// ---------- move choice ----------

uint8_t aiChooseMove(const Combatant &self, const Combatant &foe, bool smart) {
  uint8_t legal[MOVE_SLOTS], n = 0;
  for (int i = 0; i < MOVE_SLOTS; i++)
    if (self.moves[i] && self.moves[i] < MOVE_COUNT) legal[n++] = self.moves[i];
  if (!n) return 0;
  if (!smart) return legal[random(n)];

  int32_t bestScore = -2147483647;
  uint8_t best = legal[0];

  for (uint8_t i = 0; i < n; i++) {
    uint8_t mv = legal[i];
    const MoveEntry &m = MOVE_TBL[mv];
    int32_t sc = 0;

    if (m.cat == MC_STATUS) {
      if (m.effect == EF_HEAL) {
        int32_t missing = (int32_t)self.maxHp - self.hp;
        sc = (missing * 100 / (self.maxHp ? self.maxHp : 1)) - 30;
      } else if (m.effect == EF_STAGE) {
        static const uint8_t BIT[SI_COUNT] = { ST_ATK, ST_DEF, ST_SPA, ST_SPD, ST_SPE };
        int stacked = 0, hit = 0;
        const Combatant &t = (m.target == TG_SELF) ? self : foe;
        for (int k = 0; k < SI_COUNT; k++)
          if (m.statMask & BIT[k]) { stacked += t.stage[k] * (m.stages > 0 ? 1 : -1); hit++; }
        if (!hit) hit = 1;

        sc = 26 - (stacked * 12 / hit);
        if (self.hp * 3 < self.maxHp) sc -= 40;
      } else {
        sc = 5;
      }
    } else {
      uint16_t dmg = battleDamage(self, foe, mv, false, 236);
      sc = dmg;
      if (dmg >= foe.hp) sc += 1000;
      uint8_t acc = m.acc ? m.acc : 100;
      sc = sc * acc / 100;
      if (m.effect == EF_RECHARGE) sc -= dmg / 4;
      if (m.effect == EF_RECOIL) sc -= dmg / 6;
      if (m.effect == EF_CHARGE) sc -= dmg / 3;
      if (m.ailment != AIL_NONE && foe.ailment == AIL_NONE)
        sc += m.ailChance / 4;
    }

    if (sc > bestScore) {
      bestScore = sc;
      best = mv;
    }
  }
  return best;
}
