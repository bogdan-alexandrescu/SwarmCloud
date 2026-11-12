# Talk to Voice it

Voice it lets you talk to SwarmCloud: say what you want, and it plans and starts the work. Today it is open to **platform admins** only; other people do not see it in the menu.

1. Open [Voice it › Talk](/voice/talk).
2. Press **Start talking** and allow the microphone. Say what you want.
   Prefer typing? Use **Say to Voice it** and press **Send**.
3. Press **Mute** to pause your microphone, **End** to finish.
4. Earlier conversations are on [Voice it › Projects](/voice/projects): **Resume**, **Rename** or **Delete** them.
5. Standing preferences are on [Voice it › Preferences](/voice/preferences).

There is no Voice it command in the sc plugin.

For operators: the paid voice pauses on its own while delegated work runs and reopens on the result. The switch is Terraform's `voice_pause` in `terraform/infra/voice.tf`, `"on"` by default; set it to `"off"` and apply to keep the voice open until the session ends.
