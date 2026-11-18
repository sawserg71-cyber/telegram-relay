    state["offset"] = offset
    state_changed = offset != state["offset"] or allowed_changed or not os.path.exists(STATE_FILE)
    if state_changed:
        save_json(STATE_FILE, state)
        save_json(ALLOWED_FILE, allowed)
    print("poll: отправлено писем: %d, подтверждено пользователей: %d" % (sent, len(allowed["users"])))
