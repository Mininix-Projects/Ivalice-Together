import unittest

import launcher_i18n as i18n


class LauncherI18nTests(unittest.TestCase):
    def test_every_direct_mapping_round_trips_to_portuguese(self):
        for source, target in i18n._ENGLISH.items():
            with self.subTest(source=source):
                self.assertTrue(target)
                self.assertEqual(i18n.translate(source, "en"), target)
                self.assertEqual(i18n.translate(source, "pt"), source)

    def test_language_normalization_and_default(self):
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "en")
        self.assertEqual(i18n.normalize_language(None), "en")
        self.assertEqual(i18n.normalize_language("unknown"), "en")
        self.assertEqual(i18n.normalize_language("EN_us"), "en")
        self.assertEqual(i18n.normalize_language("pt-BR"), "pt")
        self.assertEqual(i18n.normalize_language("Português"), "pt")

    def test_portuguese_and_unknown_text_are_preserved(self):
        source = "Formação salva para a próxima sessão."
        self.assertIs(i18n.translate(source, "pt"), source)
        self.assertEqual(i18n.translate("USB Arcade Stick", "en"), "USB Arcade Stick")

    def test_core_navigation_action_and_theme_labels(self):
        expected = {
            "FORMAÇÃO": "PARTY",
            "CONTROLES": "CONTROLS",
            "Avançar": "Forward",
            "Trocar personagem": "Change character",
            "Party / Licenças": "Party / Licenses",
            "Safira": "Sapphire",
            "Ouro": "Gold",
            "Esmeralda": "Emerald",
            "Ametista": "Amethyst",
            "Rosa": "Pink",
            "Ônix": "Onyx",
            "Bronze": "Bronze",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)

    def test_page_copy_and_settings_copy(self):
        expected = {
            "COMEÇAR COM TODOS OS PERSONAGENS": "START WITH ALL CHARACTERS",
            "Cores da party": "Party colors",
            "Controles de cada jogador": "Controls for each player",
            "Ajustes do configurador": "Configurator settings",
            "Idioma": "Language",
            "Escolha o idioma da ferramenta.": "Choose the launcher language.",
            "Escolha o idioma usado em toda a interface.": "Choose the language used throughout the interface.",
            "Idioma alterado e salvo.": "Language changed and saved.",
            "Pressione uma tecla. Botão direito cancela.": "Press a key. Right-click to cancel.",
            "Pressione uma tecla": "Press a key",
            "MOUSE: selecionar e configurar · BOTÃO DIREITO: cancelar ou limpar": "MOUSE: select and configure · RIGHT-CLICK: cancel or clear",
            "SEM CONTROLE": "NO CONTROLLER",
            "2 · TECLADO + MOUSE · 1 DISPOSITIVO": "2 · KEYBOARD + MOUSE · 1 DEVICE",
            "2 · TECLADO + MOUSE": "2 · KEYBOARD + MOUSE",
            "SEM DONO": "NO OWNER",
            "MOVER TRANSFERE AMBOS": "MOVE TRANSFERS BOTH",
            "3 · AÇÕES E ATALHOS (OPCIONAL)": "3 · ACTIONS AND BINDINGS (OPTIONAL)",
            "Clique em um cartão para trocar a paleta · as escolhas do modo inativo são preservadas": "Click a card to change its palette · inactive-mode choices are preserved",
            "ATIVO": "ON",
            "DESL.": "OFF",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)

    def test_dynamic_player_binding_and_controller_messages(self):
        expected = {
            "VERSÃO 1.1.1": "VERSION 1.1.1",
            "Membro 7": "Member 7",
            "JOGADOR 3": "PLAYER 3",
            "JOGADOR 2 · Basch": "PLAYER 2 · Basch",
            "Controle 4": "Controller 4",
            "Pressione START no controle de P2.": "Press START on P2's controller.",
            "P3 agora está sem controle. Salve para aplicar.": "P3 now has no controller. Save to apply.",
            "Use o Controle 2, atribuído a P2.": "Use Controller 2, assigned to P2.",
            "Controle 3 atribuído a P3.": "Controller 3 assigned to P3.",
            "Controle 2 atribuído a P2; o controle anterior de P2 foi movido para P1.": "Controller 2 assigned to P2; P2's previous controller was moved to P1.",
            "DualSense atribuído a P3; o controle anterior de P3 foi movido para P2.": "DualSense assigned to P3; P3's previous controller was moved to P2.",
            "Trocar personagem  ·  ATALHO 2": "Change character  ·  BINDING 2",
            "Controle não detectado  ·  XINPUT 4": "Controller not detected  ·  XINPUT 4",
            "DONO: P2": "OWNER: P2",
            "DONO ÚNICO: P2": "SOLE OWNER: P2",
            "Teclado e mouse atribuídos a P1.": "Keyboard and mouse assigned to P1.",
            "Teclado e mouse transferidos de P2 para P3.": "Keyboard and mouse transferred from P2 to P3.",
            "Teclado e mouse já pertencem a P1.": "Keyboard and mouse already belong to P1.",
            "Teclado e mouse pertencem a P2. Transfira o dispositivo compartilhado antes de usar uma tecla em P3.": "Keyboard and mouse belong to P2. Transfer the shared device before using a key for P3.",
            "Teclado e mouse não estão atribuídos. Atribua o dispositivo compartilhado a P1 antes de usar uma tecla.": "Keyboard and mouse are unassigned. Assign the shared device to P1 before using a key.",
            "Configuração de controles atualizada: teclado e mouse agora pertencem a P2. Migração salva.": "Controls configuration updated: keyboard and mouse now belong to P2. Migration saved.",
            "Teclado e mouse estavam atribuídos a mais de um jogador. Mantido em P1; removido de P2, P3. Correção salva.": "Keyboard and mouse were assigned to more than one player. Kept for P1; removed from P2, P3. Repair saved.",
            "O arquivo de controles usa a versão 10, mas este configurador suporta até a versão 9. O arquivo foi preservado sem alterações; atualize o mod para editá-lo.": "The controls file uses version 10, but this configurator supports up to version 9. The file was preserved unchanged; update the mod to edit it.",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)

    def test_error_prefixes_preserve_technical_detail(self):
        expected = {
            "Não foi possível salvar a formação: access denied": "Could not save the party: access denied",
            "Não foi possível salvar as cores: disk full": "Could not save the colors: disk full",
            "Não foi possível salvar antes de iniciar: read only": "Could not save before starting: read only",
            "Não foi possível salvar: E_FAIL": "Could not save: E_FAIL",
            "Não foi possível salvar a opção de elenco: access denied": "Could not save the roster option: access denied",
            "Controle capturado, mas não foi possível aplicar: disk full": "Controller captured, but it could not be applied: disk full",
            "Não foi possível zerar a sessão de dano: read only": "Could not reset the damage session: read only",
            "co-op encerrou com código 2: sem detalhes": "co-op exited with code 2: no details",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)

    def test_launcher_core_messages(self):
        expected = {
            "Nenhuma captura ativa.": "No capture is active.",
            "O Jogador 1 usa apenas um controle XInput.": "Player 1 can only use an XInput controller.",
            "Comando alterado. Salve para aplicar.": "Binding changed. Save to apply.",
            "Co-op ativo. Boa viagem por Ivalice.": "Co-op active. Safe travels through Ivalice.",
            "Sessão co-op encerrada.": "Co-op session ended.",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)

    def test_dirty_markers_are_preserved(self):
        self.assertEqual(
            i18n.translate("SALVAR FORMAÇÃO  •", "en"),
            "SAVE PARTY  •",
        )
        self.assertEqual(
            i18n.translate("SALVAR E APLICAR  •", "en"),
            "SAVE AND APPLY  •",
        )

    def test_reattach_messages_default_to_english(self):
        expected = {
            "Sessão co-op encontrada. Conectando controles e interface…": "Co-op session found. Connecting controls and interface…",
            "Co-op já ativo. Launcher reconectado à sessão.": "Co-op is already active. Launcher reattached to the session.",
            "Co-op ativo. Launcher reconectado à sessão.": "Co-op active. Launcher reattached to the session.",
            "A sessão co-op foi encerrada. Pronto para ligar novamente.": "The co-op session ended. Ready to start it again.",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)

    def test_external_notices_and_runtime_progress_default_to_english(self):
        expected = {
            "O configurador já está aberto. Procure o ícone do co-op perto do relógio.": "The configurator is already open. Look for the co-op icon in the system tray.",
            "Sessão encontrada. Confirmando controles e interface…": "Session found. Confirming controls and interface…",
            "Co-op ativo. Pode carregar ou continuar o jogo.": "Co-op is active. You can load or continue the game.",
            "Núcleo conectado. Carregue ou continue um save para concluir a validação.": "Core connected. Load or continue a save to complete validation.",
            "Save carregado. Confirmando controles, HUD e cores…": "Save loaded. Confirming controls, HUD, and colors…",
            "Pressione uma tecla ou botão. Botão direito cancela.": "Press a key or button. Right-click to cancel.",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)
                self.assertEqual(i18n.translate(source, "pt"), source)

    def test_installation_errors_are_translated_without_losing_details(self):
        expected = {
            "a pilha de loaders carregada não é utilizável: required loader is not active: ff12-file-loader.dll": "loaded loader stack is unusable: required loader is not active: ff12-file-loader.dll",
            "co-op nao confirmou o contrato final: damage=0 loot=1": "co-op did not confirm its final runtime contract: damage=0 loot=1",
            "A instalação do jogo está incompleta. Arquivos ausentes: x64/a.dll": "The game installation is incomplete. Missing files: x64/a.dll",
            "Microsoft Visual C++ 2015-2022 Redistributable (x64) ausente. Instale-o e tente novamente. DLLs ausentes: VCRUNTIME140.dll": "Microsoft Visual C++ 2015-2022 Redistributable (x64) is missing. Install it and try again. Missing DLLs: VCRUNTIME140.dll",
            "x64\\hook.ini usa carregamento explicito, mas nao lista todos os modulos do Ivalice Together: modules\\a.dll": "x64\\hook.ini uses explicit loading but does not list every Ivalice Together module: modules\\a.dll",
            "Esta versão de FFXII_TZA.exe não é compatível com o runtime atual do Ivalice Together (SHA-256 observado: ABCD).": "This FFXII_TZA.exe version is not compatible with the current Ivalice Together runtime (observed SHA-256: ABCD).",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.translate(source, "en"), target)


if __name__ == "__main__":
    unittest.main()
