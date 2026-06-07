from telethon import TelegramClient, events
import asyncio, random, re

# --- CONFIG ---
api_id = 34572696
api_hash = "01b3e8fe2abbd5f63137121d235fd467"
BOT = "@Pokepixelbot"
ALERT_ID = 8354083370
GROUP_ID = 1003863984988

client = TelegramClient("session", api_id, api_hash)

# --- GLOBAL STATE ---
pokemon_alive      = False
running            = True
paused             = False
stop_list          = []
farm_mode          = False
farm_task          = None
farm_last_name     = None
last_mode          = None
auto_restart_task  = None
user_acted         = False

battle_failed_moves = set()
last_move_used      = None

# --- CATCH STATE ---
catching_pokemon    = False   # stop-list poke battle mein hai
catch_poke_name     = ""
catch_poke_level    = 0
catch_total_attempts = 0
catch_tier          = 0       # 0=Pokeball 1=Great Ball 2=Ultra Ball
catch_tier_used     = 0       # current tier mein kitni baar throw ki
catch_waiting_result = False  # True = ball throw ho chuki, result ka wait
catch_mode          = False   # /pc=True auto catch | /pa=False alert+pause


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def normalize(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', name).lower()


def get_ball_tier(level: int) -> int:
    if level <= 25:  return 0
    elif level <= 35: return 1
    else:             return 2


def tier_to_ball(tier: int) -> str:
    return ["pokeball", "great ball", "ultra ball"][min(tier, 2)]


def has_ball_buttons(event) -> bool:
    """Sirf actual pokeball buttons — move names (Shadow Ball etc.) ignore"""
    if not event.buttons:
        return False
    exact = ["pokeball", "poke ball", "great ball", "ultra ball"]
    for row in event.buttons:
        for btn in row:
            if btn and btn.text:
                t = btn.text.lower()
                if any(k in t for k in exact):
                    return True
    return False


def is_bag_screen(text: str, event) -> bool:
    """Text ya buttons se bag screen confirm karo"""
    if "choose a ball" in text or "battle medicine" in text:
        return True
    return has_ball_buttons(event)


def extract_wild_pokemon(raw_text: str):
    """
    Pokemon naam aur level extract karo wild message se.
    Returns: (name_str, level_int)
    """
    # Pattern 1: "A wild Pikachu (Lv. 12) has appeared"
    m = re.search(
        r'[Aa] wild\s+([A-Za-z][A-Za-z\'\- ]{1,30}?)\s*\(Lv\.?\s*(\d+)\)',
        raw_text
    )
    if m:
        return m.group(1).strip(), int(m.group(2))

    # Pattern 2: naam stop karo jab "has"/"appeared"/"!" mile
    m2 = re.search(
        r'[Aa] wild\s+([A-Za-z][A-Za-z\'\- ]{1,30}?)(?=\s+(?:has|appeared|!|\())',
        raw_text
    )
    if m2:
        name = m2.group(1).strip()
        # Level alag se dhundho
        lm = re.search(r'Lv\.?\s*(\d+)', raw_text, re.IGNORECASE)
        level = int(lm.group(1)) if lm else 1
        return name, level

    # Pattern 3: sirf pehle 1-2 capitalized words
    m3 = re.search(r'[Aa] wild\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)', raw_text)
    if m3:
        name = m3.group(1).strip()
        lm = re.search(r'Lv\.?\s*(\d+)', raw_text, re.IGNORECASE)
        level = int(lm.group(1)) if lm else 1
        return name, level

    return "", 1


def poke_matches_target(poke_name: str, target: str) -> bool:
    """Flexible match — exact ya partial"""
    pn = normalize(poke_name)
    tn = normalize(target)
    return tn == pn or tn in pn


def reset_catch_state():
    global catching_pokemon, catch_poke_name, catch_poke_level
    global catch_total_attempts, catch_tier, catch_tier_used, catch_waiting_result
    catching_pokemon     = False
    catch_poke_name      = ""
    catch_poke_level     = 0
    catch_total_attempts = 0
    catch_tier           = 0
    catch_tier_used      = 0
    catch_waiting_result = False


# ─────────────────────────────────────────────
#  NETWORK / IO
# ─────────────────────────────────────────────

async def is_bot_dm(event):
    try:
        chat = await event.get_chat()
        username = getattr(chat, "username", None)
        return username and username.lower() == BOT.replace("@", "").lower()
    except:
        return False


async def send_to_group(msg: str):
    try:
        await client.send_message(GROUP_ID, msg)
    except Exception as e:
        print(f"Group send error: {e}")


# ─────────────────────────────────────────────
#  AUTO RESTART TIMER (4 min)
# ─────────────────────────────────────────────

async def auto_restart_timer():
    global paused, running, farm_mode, farm_task, last_mode, user_acted

    await asyncio.sleep(240)  # 4 minutes

    if user_acted or not paused:
        return

    await send_to_group("⏱️ 4 min ho gaye — Auto Restart ho raha hai!")

    paused = False
    running = True

    if last_mode == "farm":
        farm_mode = True
        if farm_task:
            farm_task.cancel()
        farm_task = asyncio.create_task(farm_loop())
        await send_to_group("🌾 Farming Auto Restarted!")
    else:
        farm_mode = False
        await client.send_message(BOT, "/hunt")
        await send_to_group("▶️ Hunting Auto Restarted!")


# ─────────────────────────────────────────────
#  PAUSE
# ─────────────────────────────────────────────

async def send_pause(reason, alert_msg=None, start_restart_timer=False):
    global paused, running, farm_mode, farm_task, auto_restart_task, user_acted

    paused    = True
    running   = False
    farm_mode = False
    if farm_task:
        farm_task.cancel()

    extra = "\n⏱️ 4 min mein auto restart hoga" if start_restart_timer \
            else "\n🔒 Manual resume zaroori hai"

    await send_to_group(
        f"{reason}\n\n"
        f"🛑 Script Paused\n"
        f"▶️ /pchalo to resume\n"
        f"🌾 /pfarm to start farming\n\n"
        f"Bhagooo ➜ @Pokepixelbot\n\n{extra}"
    )

    if alert_msg:
        try:
            await client.send_message(ALERT_ID, alert_msg)
        except Exception as e:
            print(f"Alert failed: {e}")

    if start_restart_timer:
        user_acted = False
        if auto_restart_task:
            auto_restart_task.cancel()
        auto_restart_task = asyncio.create_task(auto_restart_timer())


# ─────────────────────────────────────────────
#  RESUME
# ─────────────────────────────────────────────

async def resume_bot():
    global paused, running
    paused  = False
    running = True
    await client.send_message(BOT, "/hunt")


# ─────────────────────────────────────────────
#  FARM LOOP
# ─────────────────────────────────────────────

async def farm_loop():
    global farm_mode, paused, farm_last_name

    while farm_mode:
        if paused:
            await asyncio.sleep(0.3)
            continue

        farm_last_name = None
        try:
            await client.send_message(BOT, "/hunt")
        except:
            pass

        pokemon_received = False
        for _ in range(12):
            if not farm_mode or paused:
                break
            await asyncio.sleep(0.5)
            if farm_last_name:
                pokemon_received = True
                break

        if not pokemon_received or not farm_last_name:
            continue

        poke_name = farm_last_name
        farm_last_name = None

        for target in stop_list:
            if poke_matches_target(poke_name, target):
                try:
                    await client.send_message(ALERT_ID, f"🎯 FARM ALERT: {poke_name} FOUND!")
                except:
                    pass
                await send_pause(
                    f"🎯 Target Pokémon Found: {poke_name}",
                    None,
                    start_restart_timer=True
                )
                return

        await asyncio.sleep(random.uniform(0.8, 1.0))


# ─────────────────────────────────────────────
#  SMART CLICK
# ─────────────────────────────────────────────

async def smart_click(event, btn_text: str, retries: int = 3) -> bool:
    for _ in range(retries):
        try:
            msg = await client.get_messages(event.chat_id, ids=event.id)
            if not msg or not msg.buttons:
                await asyncio.sleep(0.2)
                continue

            target = (btn_text or "").lower().strip()

            for i, row in enumerate(msg.buttons):
                for j, btn in enumerate(row):
                    if not btn or not btn.text:
                        continue
                    btn_raw = btn.text.lower().strip()
                    if target in btn_raw or btn_raw in target:
                        await msg.click(i, j)
                        return True
        except:
            pass
        await asyncio.sleep(0.25)
    return False


# ─────────────────────────────────────────────
#  THROW POKEBALL  (single throw, tier logic)
# ─────────────────────────────────────────────

async def throw_pokeball(event):
    global catch_total_attempts, catch_tier, catch_tier_used
    global catching_pokemon, catch_waiting_result

    ball_name = tier_to_ball(catch_tier)

    # Try intended tier
    clicked = await smart_click(event, ball_name)

    # Fallback chain: ultra → great → pokeball
    if not clicked and catch_tier >= 2:
        clicked = await smart_click(event, "great ball")
    if not clicked and catch_tier >= 1:
        clicked = await smart_click(event, "pokeball")

    if not clicked:
        await send_to_group(
            f"⚠️ Koi bhi Pokeball nahi bachi!\n"
            f"❌ {catch_poke_name} catch nahi ho sakta."
        )
        reset_catch_state()
        return

    # Ball throw successful
    catch_total_attempts += 1
    catch_tier_used      += 1
    catch_waiting_result  = True   # ab result ka wait karo

    # 2 throws ke baad tier upgrade
    if catch_tier_used >= 2:
        catch_tier      = min(catch_tier + 1, 2)
        catch_tier_used = 0


# ─────────────────────────────────────────────
#  DETECT HELPERS
# ─────────────────────────────────────────────

def is_tm_found(text):
    return bool(re.search(r'you found tm\d+', text, re.IGNORECASE))

def is_coins_found(text):
    return bool(re.search(
        r'(you (?:found|picked up) \d[\d,]* (?:shiny )?pok[eé]coins?'
        r'|hidden stash revealed \d[\d,]* pok[eé]coins?'
        r'|stumbled upon \d[\d,]* (?:shiny )?pok[eé]coins?'
        r'|found \d[\d,]* (?:shiny )?pok[eé]coins?)',
        text, re.IGNORECASE
    ))

def is_gigantamax(text):
    return bool(re.search(r'a gigantamax pok[eé]mon is approaching', text, re.IGNORECASE))

def is_wild_shiny(text):
    return bool(re.search(r'you found a', text, re.IGNORECASE))

def extract_pokemon_from_text(text: str):
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^\d+[\.\)]\s*(.+)', line)
        if m:
            name = re.sub(r'[^\w\s]', '', m.group(1)).strip()
            if name: names.append(name)
            continue
        m = re.match(r'^[•\-\*]\s*(.+)', line)
        if m:
            name = re.sub(r'[^\w\s]', '', m.group(1)).strip()
            if name: names.append(name)
    return names


# ─────────────────────────────────────────────
#  MAIN PROCESS
# ─────────────────────────────────────────────

async def process(event):
    global pokemon_alive, running, paused, last_move_used, battle_failed_moves
    global farm_last_name, catching_pokemon, catch_poke_name, catch_poke_level
    global catch_total_attempts, catch_tier, catch_tier_used
    global catch_waiting_result, catch_mode

    if not await is_bot_dm(event):
        return

    # Thoda delay — message fully render ho jaye pehle
    await asyncio.sleep(0.2)

    raw_text = event.raw_text or ""
    text     = raw_text.lower()

    print("Bot:", raw_text[:80])

    # ── TM ──────────────────────────────────
    if is_tm_found(text):
        await asyncio.sleep(1.0)
        await client.send_message(BOT, "/hunt")
        return

    # ── COINS ───────────────────────────────
    if is_coins_found(text):
        await asyncio.sleep(1.0)
        await client.send_message(BOT, "/hunt")
        return

    # ── GIGANTAMAX ──────────────────────────
    if is_gigantamax(text):
        await send_pause(
            "🔴 Gigantamax Pokémon Aaya!\n\n⚠️ Manual handle karo!",
            "🔴 GIGANTAMAX ALERT — Turat dekho!",
            start_restart_timer=False
        )
        return

    # ── NEW MOVE ────────────────────────────
    if any(x in text for x in ["learned a new move", "wants to learn", "forgot a move"]):
        await send_pause(
            "🧠 New Move Detected!\n\nManually handle karo.\n\n@Pokepixelbot",
            "⚠️ NEW MOVE ALERT — Bot ruk gaya!",
            start_restart_timer=False
        )
        return

    if paused:
        return

    # ── SHINY ───────────────────────────────
    if is_wild_shiny(text):
        m = re.search(r"A wild ([^(]+)", raw_text, re.IGNORECASE)
        poke_name = m.group(1).strip() if m else "Shiny Pokémon"
        await send_pause(
            f"✨ Shiny Pokémon Mila: {poke_name}\n\n⚠️ Manual handle karo!",
            f"✨ SHINY ALERT: {poke_name} FOUND!",
            start_restart_timer=False
        )
        return

    # ── FARM MODE ───────────────────────────
    if farm_mode:
        if ("a wild" in text or "appeared" in text) and \
           not ("hidden" in text or "stumbled" in text):
            m = re.search(r"A wild ([^(]+)", raw_text, re.IGNORECASE)
            poke_name = m.group(1).strip() if m else ""
            if poke_name:
                farm_last_name = poke_name
        return

    # ── IGNORE ITEM MESSAGES ────────────────
    if ("a wild" in text or "appeared" in text) and \
       ("hidden" in text or "stumbled" in text):
        return

    # ── EVOLVE ──────────────────────────────
    if "ready to evolve" in text or "wants to evolve" in text:
        if event.buttons:
            for row in event.buttons:
                for btn in row:
                    if btn and "evolve" in btn.text.lower():
                        await smart_click(event, btn.text)
                        return

    # ════════════════════════════════════════
    #  WILD POKEMON APPEARED
    # ════════════════════════════════════════
    if ("a wild" in text or "appeared" in text) and \
       not ("hidden" in text or "stumbled" in text):

        battle_failed_moves.clear()
        reset_catch_state()   # har naye encounter pe fresh start

        poke_name, poke_level = extract_wild_pokemon(raw_text)

        # Ancient check
        if "ancient" in text:
            await send_pause(
                f"🏛️ Ancient Pokemon Mila!\n━━━━━━━━━━━━━━━\n🎯 Jao aur pakdo!",
                f"🏛️ ANCIENT ALERT: {poke_name} FOUND!",
                start_restart_timer=True
            )
            return

        # Stop list check
        for target in stop_list:
            if poke_matches_target(poke_name, target):
                if catch_mode:
                    # AUTO CATCH
                    catching_pokemon     = True
                    catch_poke_name      = poke_name
                    catch_poke_level     = poke_level
                    catch_total_attempts = 0
                    catch_tier           = get_ball_tier(poke_level)
                    catch_tier_used      = 0
                    catch_waiting_result = False
                    pokemon_alive        = True

                    await asyncio.sleep(0.5)  # message settle hone do
                    await smart_click(event, "Capture")
                    return
                else:
                    # ALERT + PAUSE
                    ball_start = tier_to_ball(get_ball_tier(poke_level))
                    await send_pause(
                        f"🎯 Target Pokemon Mila!\n━━━━━━━━━━━━━━━\n"
                        f"• {poke_name} (Lv.{poke_level})\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"🎾 Suggested: {ball_start}",
                        f"🎯 HUNT ALERT: {poke_name} FOUND!",
                        start_restart_timer=True
                    )
                    return

        # Normal wild — Capture click
        pokemon_alive = True
        await asyncio.sleep(0.3)
        await smart_click(event, "Capture")
        return

    # ════════════════════════════════════════
    #  CATCHING MODE — result wait
    # ════════════════════════════════════════
    if catching_pokemon and catch_waiting_result:
        # Caught?
        if "level:" in text and "nature:" in text and "need to next level:" in text:
            await send_to_group(f"✅ {catch_poke_name} Successfully Caught! 🎉")
            reset_catch_state()
            pokemon_alive = False
            await asyncio.sleep(1.0)
            await client.send_message(BOT, "/hunt")
            return

        # Failed? → Bag pe wapis jao
        if "failed" in text and event.buttons:
            catch_waiting_result = False
            if catch_total_attempts >= 7:
                await send_to_group(
                    f"❌ {catch_poke_name} catch nahi hua\n7 balls khatam!"
                )
                reset_catch_state()
                await asyncio.sleep(0.5)
                await client.send_message(BOT, "/hunt")
                return
            await asyncio.sleep(random.uniform(0.5, 0.8))
            bag_ok = await smart_click(event, "Bag")
            if not bag_ok:
                await smart_click(event, "Capture")
            return

        # * animation ya kuch aur — ignore karo
        return

    # ── AUTO RESTART ────────────────────────
    if any(x in text for x in [
        "you got defeated", "fled", "fainted.", "learnt",
        "time's up", "time up"
    ]):
        reset_catch_state()
        pokemon_alive = False
        await asyncio.sleep(0.8)
        await client.send_message(BOT, "/hunt")
        return

    # ── CAUGHT (safety net) ─────────────────
    if catching_pokemon and \
       "level:" in text and "nature:" in text and "need to next level:" in text:
        await send_to_group(f"✅ {catch_poke_name} Successfully Caught! 🎉")
        reset_catch_state()
        pokemon_alive = False
        await asyncio.sleep(1.0)
        await client.send_message(BOT, "/hunt")
        return

    # ── FAIL MOVE DETECT ────────────────────
    if last_move_used and any(x in text for x in [
        "dealt 0 damage", "0 damage", "no effect",
        "it had no effect", "no pp left!", "absorbed", "was absorbed"
    ]):
        battle_failed_moves.add(last_move_used)
        last_move_used = None

    # ── SWITCH ──────────────────────────────
    if "choose which poke send for battle" in text or "choose" in text:
        if event.buttons:
            all_btns = []
            for row in event.buttons:
                for btn in row:
                    if btn and btn.text:
                        t = btn.text.lower().strip()
                        if "run" in t or "empty" in t:
                            continue
                        all_btns.append(btn.text)
            if all_btns:
                await smart_click(event, random.choice(all_btns))
                return

    # ════════════════════════════════════════
    #  MOVES / BAG SECTION
    # ════════════════════════════════════════
    if not (pokemon_alive and running and event.buttons):
        return

    # ── CATCHING MODE ─── (move kabhi nahi dabega)
    if catching_pokemon:
        await asyncio.sleep(random.uniform(0.5, 0.8))

        if is_bag_screen(text, event):
            # Ball screen — throw
            if catch_total_attempts < 7:
                await throw_pokeball(event)
            else:
                await send_to_group(
                    f"❌ {catch_poke_name} — 7 balls khatam!"
                )
                reset_catch_state()
                await client.send_message(BOT, "/hunt")
        else:
            # Battle screen — Bag ya Capture click karo
            ok = await smart_click(event, "Bag")
            if not ok:
                await smart_click(event, "Capture")
        return   # ← hamesha return — move kabhi click nahi

    # ── NORMAL BATTLE ───────────────────────
    await asyncio.sleep(random.uniform(0.5, 0.8))

    moves = []

    # Format 1:  - Name [Type]\nPower: X, Accuracy: Y
    p1 = re.findall(
        r'-\s*(.*?)\s*\[.*?\]\s*\nPower\s*:\s*(\w+).\s*Accuracy\s*:\s*(\d+)',
        raw_text, re.IGNORECASE
    )
    for name, power, acc in p1:
        nc = name.lower().strip()
        pw = 0 if power.lower() == 'null' else int(power)
        if nc in battle_failed_moves or pw == 0: continue
        moves.append({"name": nc, "text": name, "score": pw + int(acc)})

    # Format 2:  emoji Name • power⚔️ • acc%
    if not moves:
        p2 = re.findall(
            r'.+?\s+([\w][^\•\n]+?)\s*•\s*(\d+)[^•\n]*•\s*(\d+)%',
            raw_text, re.IGNORECASE
        )
        for name, power, acc in p2:
            nc = name.lower().strip()
            pw, ac = int(power), int(acc)
            if nc in battle_failed_moves or pw == 0 or ac == 0: continue
            moves.append({"name": nc, "text": name.strip(), "score": pw + ac})

    # Format 3:  Name • Pwr X • Acc Y% • Physical
    if not moves:
        p3 = re.findall(
            r'^([A-Za-z][A-Za-z ]+?)\s*•\s*Pwr\s+(\d+)\s*•\s*Acc\s+(\d+)%',
            raw_text, re.IGNORECASE | re.MULTILINE
        )
        for name, power, acc in p3:
            nc = name.lower().strip()
            pw, ac = int(power), int(acc)
            if nc in battle_failed_moves or pw == 0 or ac == 0: continue
            moves.append({"name": nc, "text": name.strip(), "score": pw + ac})

    moves.sort(key=lambda x: x["score"], reverse=True)

    if moves:
        best = moves[0]
        last_move_used = best["name"]
        while pokemon_alive and running:
            if await smart_click(event, best["text"]):
                break
            await asyncio.sleep(random.uniform(0.3, 0.6))


# ─────────────────────────────────────────────
#  EVENT HANDLERS
# ─────────────────────────────────────────────

@client.on(events.NewMessage(from_users=BOT))
async def new_handler(event):
    await process(event)

@client.on(events.MessageEdited(from_users=BOT))
async def edit_handler(event):
    await process(event)


# ─────────────────────────────────────────────
#  GROUP COMMANDS
# ─────────────────────────────────────────────

@client.on(events.NewMessage(chats=GROUP_ID))
async def group_commands(event):
    global paused, running, stop_list, farm_mode, farm_task
    global last_mode, auto_restart_task, user_acted, catch_mode

    if not event.out:
        return

    text = (event.raw_text or "").strip()
    cmd  = text.lower()

    user_acted = True
    if auto_restart_task:
        auto_restart_task.cancel()
        auto_restart_task = None

    # ── HUNT ────────────────────────────────
    if cmd in ["/pchalo", "/hunt"]:
        last_mode = "hunt"
        farm_mode = False
        await resume_bot()
        await send_to_group("▶️ Hunting Started!")

    # ── PAUSE ───────────────────────────────
    elif cmd == "/ppause":
        paused    = True
        running   = False
        farm_mode = False
        if farm_task: farm_task.cancel()
        await send_to_group(
            "⏸️ Script Paused!\n\n"
            "▶️ /pchalo  ➜ Resume karo\n"
            "🌾 /pfarm   ➜ Farming shuru karo"
        )

    # ── CATCH MODE ON ───────────────────────
    elif cmd == "/pc":
        catch_mode = True
        await send_to_group(
            "🎾 Catch Mode ON!\n"
            "━━━━━━━━━━━━━━━\n"
            "Stop list ke pokemon\n"
            "automatically catch honge\n"
            "━━━━━━━━━━━━━━━\n"
            "🔕 Koi alert nahi ayega\n"
            "❌ /pa se alert mode wapis"
        )

    # ── ALERT MODE ON ───────────────────────
    elif cmd == "/pa":
        catch_mode = False
        await send_to_group(
            "🔔 Alert Mode ON!\n"
            "━━━━━━━━━━━━━━━\n"
            "Stop list ke pokemon\n"
            "milne pe pause + alert hoga\n"
            "━━━━━━━━━━━━━━━\n"
            "🎾 /pc se catch mode on karo"
        )

    # ── STATUS ──────────────────────────────
    elif cmd == "/pstatus":
        if farm_mode:
            hunt_status = "🌾 Farming Mode"
        elif running and not paused:
            hunt_status = "🏃 Hunting Mode"
        else:
            hunt_status = "⏸️ Paused"

        list_status = "🎾 Catch List (auto catch)" \
                      if catch_mode else "🔔 Alert List (pause + alert)"
        poke_list = "\n".join(
            f"  {i}. {p}" for i, p in enumerate(stop_list, 1)
        ) if stop_list else "  Empty"

        await send_to_group(
            f"📊 Bot Status\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Mode: {hunt_status}\n"
            f"List: {list_status}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📋 Pokemon List:\n{poke_list}\n"
            f"━━━━━━━━━━━━━━━"
        )

    # ── FARM ────────────────────────────────
    elif cmd == "/pfarm":
        last_mode = "farm"
        paused    = False
        running   = True
        farm_mode = True
        if farm_task: farm_task.cancel()
        farm_task = asyncio.create_task(farm_loop())
        await send_to_group(
            "🌾 Farming Started!\n\n"
            "⚡ Fast Mode ON\n"
            "🎯 Stop List Active\n"
            "🛑 Target mile toh rukega"
        )

    elif cmd == "/psfarm":
        farm_mode = False
        if farm_task: farm_task.cancel()
        await send_to_group("🛑 Farming Stopped!")

    # ── STOP LIST ───────────────────────────
    elif cmd.startswith("/padd "):
        name = text[6:].strip()
        if name:
            stop_list.append(name)
            await send_to_group(
                f"✅ Added!\n━━━━━━━━━━━━━━━\n"
                f"• {name}\n━━━━━━━━━━━━━━━\n"
                f"Total: {len(stop_list)} Pokémon"
            )

    elif cmd.startswith("/premove"):
        arg = text.replace("/premove", "").strip()
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(stop_list):
                removed = stop_list.pop(idx)
                await send_to_group(
                    f"❌ Removed!\n━━━━━━━━━━━━━━━\n"
                    f"• {removed}\n━━━━━━━━━━━━━━━\n"
                    f"Total: {len(stop_list)} Pokémon"
                )
            else:
                await send_to_group("⚠️ Invalid number!")
        else:
            removed = next((x for x in stop_list if normalize(x) == normalize(arg)), None)
            if removed:
                stop_list.remove(removed)
                await send_to_group(
                    f"❌ Removed!\n━━━━━━━━━━━━━━━\n"
                    f"• {removed}\n━━━━━━━━━━━━━━━\n"
                    f"Total: {len(stop_list)} Pokémon"
                )
            else:
                await send_to_group("⚠️ Not found in list!")

    elif cmd == "/plist":
        if not stop_list:
            await send_to_group(
                "📋 Stop List\n━━━━━━━━━━━━━━━\n"
                "  Empty hai abhi!\n━━━━━━━━━━━━━━━\n"
                "💡 /padd [naam] se add karo"
            )
        else:
            msg = "📋 Stop List\n━━━━━━━━━━━━━━━\n"
            for i, name in enumerate(stop_list, 1):
                msg += f"  {i}.  {name}\n"
            msg += f"━━━━━━━━━━━━━━━\nTotal: {len(stop_list)} Pokémon"
            await send_to_group(msg)

    elif cmd == "/pclear":
        count = len(stop_list)
        stop_list.clear()
        await send_to_group(
            f"🗑️ Stop List Cleared!\n━━━━━━━━━━━━━━━\n"
            f"❌ {count} Pokémon remove hue\n━━━━━━━━━━━━━━━\n"
            f"💡 /padd se naye add karo"
        )

    elif cmd == "/paddall":
        replied = await event.get_reply_message()
        if not replied or not replied.text:
            await send_to_group(
                "⚠️ Pehle kisi list ko reply karo!\n\n"
                "📝 Example:\n┌─────────────────\n"
                "│ 1. Zeraora\n│ 2. Mewtwo\n│ 3. Gengar\n"
                "└─────────────────\nUpar wali list reply karke /paddall likho"
            )
            return

        names = extract_pokemon_from_text(replied.text)
        if not names:
            await send_to_group("❌ Koi Pokemon naam nahi mila!")
            return

        added   = []
        skipped = []
        for name in names:
            if any(normalize(x) == normalize(name) for x in stop_list):
                skipped.append(name)
            else:
                stop_list.append(name)
                added.append(name)

        msg = "✅ Stop List Updated!\n━━━━━━━━━━━━━━━━━━\n\n"
        if added:
            msg += f"➕ Added ({len(added)}):\n"
            for n in added: msg += f"  • {n}\n"
            msg += "\n"
        if skipped:
            msg += f"⏭️ Already Tha ({len(skipped)}):\n"
            for n in skipped: msg += f"  • {n}\n"
            msg += "\n"
        msg += "━━━━━━━━━━━━━━━━━━\n📋 Updated Stop List:\n\n"
        for i, name in enumerate(stop_list, 1):
            msg += f"  {i}.  {name}\n"
        msg += f"\n━━━━━━━━━━━━━━━━━━\nTotal: {len(stop_list)} Pokémon"
        await send_to_group(msg)

    # ── COMMANDS LIST ───────────────────────
    elif cmd == "/pcommand":
        await send_to_group(
            "┌──────────────────────────┐\n"
            "│    🎮  BOT  COMMANDS       │\n"
            "└──────────────────────────┘\n\n"
            "🔫  HUNTING\n"
            "┌──────────────────────────\n"
            "│ /pchalo   ➜ Hunting shuru\n"
            "│ /ppause   ➜ Script rok do\n"
            "└──────────────────────────\n\n"
            "🌾  FARMING\n"
            "┌──────────────────────────\n"
            "│ /pfarm    ➜ Fast farm shuru\n"
            "│ /psfarm   ➜ Farm band karo\n"
            "└──────────────────────────\n\n"
            "🎯  STOP LIST\n"
            "┌──────────────────────────\n"
            "│ /padd [naam]    ➜ Add karo\n"
            "│ /premove [naam] ➜ Naam se\n"
            "│ /premove [no.]  ➜ No. se\n"
            "│ /plist          ➜ List dekho\n"
            "│ /pclear         ➜ Saaf karo\n"
            "│ /paddall        ➜ List reply\n"
            "└──────────────────────────\n\n"
            "🎾  CATCH MODE\n"
            "┌──────────────────────────\n"
            "│ /pc  ➜ Auto catch ON\n"
            "│ /pa  ➜ Alert mode ON\n"
            "└──────────────────────────\n\n"
            "📊  INFO\n"
            "┌──────────────────────────\n"
            "│ /pstatus ➜ Bot ki status\n"
            "│ /pcommand➜ Ye menu\n"
            "└──────────────────────────\n\n"
            "⚡  AUTO\n"
            "┌──────────────────────────\n"
            "│ TM/Coins  ➜ Auto /hunt\n"
            "│ New Move  ➜ Pause+alert\n"
            "│ Gmax/Shiny➜ Pause+alert\n"
            "│ Ancient   ➜ Pause+alert\n"
            "│ Target    ➜ Pause/Catch\n"
            "│           ➜ 4min restart\n"
            "└──────────────────────────"
        )


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

async def main():
    print("Bot starting...")
    await client.start()
    print("Bot started!")
    await send_to_group("✅ Script Started! /pcommand dekho.")
    await client.send_message(BOT, "/hunt")
    while True:
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
