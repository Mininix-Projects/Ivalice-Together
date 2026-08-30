from __future__ import annotations

import re
from types import MappingProxyType


DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "pt")


_ENGLISH = MappingProxyType(
    {
        "FORMAÇÃO": "PARTY",
        "CORES": "COLORS",
        "CONTROLES": "CONTROLS",
        "AJUSTES": "SETTINGS",
        "SESSÃO": "SESSION",
        "IDENTIDADE": "IDENTITY",
        "ENTRADAS": "INPUTS",
        "SISTEMA": "SYSTEM",
        "STATUS DO SISTEMA": "SYSTEM STATUS",
        "AGUARDANDO JOGO": "WAITING FOR GAME",
        "LOCAL · 2–3 JOGADORES": "LOCAL · 2–3 PLAYERS",
        "Desativado": "Disabled",
        "Controle conectado": "Controller connected",
        "Controle não detectado": "Controller not detected",
        "↑↓ Navegar   ·   Enter / A Confirmar   ·   Esc / B Voltar   ·   Del / botão direito Limpar": "↑↓ Navigate   ·   Enter / A Confirm   ·   Esc / B Back   ·   Del / right-click Clear",
        "Avançar": "Forward",
        "Alternar controle": "Toggle control",
        "Recuar": "Back",
        "Mover à esquerda": "Move left",
        "Mover à direita": "Move right",
        "Andar / correr": "Walk / run",
        "Fuga": "Flee",
        "Menu acima": "Menu up",
        "Menu abaixo": "Menu down",
        "Menu anterior": "Menu left",
        "Menu seguinte": "Menu right",
        "Alvo anterior": "Previous target",
        "Próximo alvo": "Next target",
        "Inimigos / equipe": "Foes / party",
        "Trocar personagem": "Change character",
        "Ativar Quickening": "Activate Quickening",
        "Confirmar / interagir": "Confirm / interact",
        "Cancelar / fechar": "Cancel / close",
        "Party / Licenças": "Party / Licenses",
        "Safira": "Sapphire",
        "Ouro": "Gold",
        "Esmeralda": "Emerald",
        "Ametista": "Amethyst",
        "Rosa": "Pink",
        "Ônix": "Onyx",
        "Rubi": "Ruby",
        "Bronze": "Bronze",
        "CONFIGURAÇÃO": "CONFIGURATION",
        "JOGO ABERTO": "GAME OPEN",
        "JOGO FECHADO": "GAME CLOSED",
        "RUNTIME INTEGRADO": "INTEGRATED RUNTIME",
        "AUTOSTART VIA MODULE LOADER": "AUTOSTART VIA MODULE LOADER",
        "SUA PARTY": "YOUR PARTY",
        "FORMAÇÃO NO JOGO": "IN-GAME PARTY",
        "ELENCO LIBERADO": "ROSTER UNLOCKED",
        "MODO HISTÓRIA": "STORY MODE",
        "Vaan começa sozinho; escolha os aliados no menu Party.": "Vaan starts solo; choose allies in the Party menu.",
        "A formação acompanha normalmente os eventos da história.": "The party follows story events normally.",
        "HISTÓRIA": "STORY",
        "ALTERAR FORMAÇÃO  ›": "CHANGE PARTY  ›",
        "Líder": "Leader",
        "Ativo": "Active",
        "Opcional": "Optional",
        "ELENCO COMPLETO NO PRIMEIRO VAAN": "FULL ROSTER WITH FIRST VAAN",
        "PREPARANDO A SESSÃO…": "PREPARING SESSION…",
        "CO-OP ATIVO": "CO-OP ACTIVE",
        "CO-OP AUTOMÁTICO": "AUTOMATIC CO-OP",
        "VERIFIQUE O AVISO ABAIXO": "CHECK THE MESSAGE BELOW",
        "Formação, cores e controles são carregados automaticamente.": "Party, colors, and controls are loaded automatically.",
        "Elenco, cores e controles são carregados automaticamente.": "Roster, colors, and controls are loaded automatically.",
        "CONFIGURE UMA VEZ, INICIE COMO PREFERIR": "CONFIGURE ONCE, START HOWEVER YOU PREFER",
        "ELENCO": "ROSTER",
        "Party e Gambits": "Party and Gambits",
        "HUD, menus e alvos": "HUD, menus, and targets",
        "XInput e DirectInput": "XInput and DirectInput",
        "CONFIGURAÇÃO SALVA NO MOD · PRONTA PARA STEAM OU MO2": "CONFIG SAVED WITH THE MOD · READY FOR STEAM OR MO2",
        "Escolha outro P2 antes de mover esse personagem para P3.": "Choose another P2 before moving this character to P3.",
        "Formação salva para a próxima sessão.": "Party saved for the next session.",
        "P1 · FIXO": "P1 · FIXED",
        "LÍDER INICIAL": "STARTING LEADER",
        "EM CAMPO": "IN PARTY",
        "RESERVA": "RESERVE",
        "Monte sua party": "Build your party",
        "ESCOLHA DIRETAMENTE QUEM JOGA": "CHOOSE EXACTLY WHO PLAYS",
        "Clique em P2 ou P3 no personagem desejado. Clique no P3 ativo outra vez para desativá-lo.": "Click P2 or P3 on the character you want. Click the active P3 again to disable it.",
        "COMEÇAR COM TODOS OS PERSONAGENS": "START WITH ALL CHARACTERS",
        "Com Vaan, todo o elenco estará disponível e os menus Party e Gambits": "With Vaan, the full roster will be available and the Party and Gambits menus",
        "serão liberados para montar o trio e configurar suas ações.": "will be unlocked so you can build the trio and configure their actions.",
        "Vaan começa solo; Party, Gambits e os cinco aliados ficam liberados para você montar no jogo.": "Vaan starts solo; Party, Gambits, and all five allies are unlocked for you to assemble in game.",
        "Vaan solo; Party, Gambits e os cinco aliados liberados.": "Vaan solo; Party, Gambits, and all five allies unlocked.",
        "Desmarcado: personagens seguem os eventos da história.": "Unchecked: characters follow story events.",
        "Elenco completo será liberado com Vaan solo.": "The full roster will be unlocked with Vaan solo.",
        "Progressão nativa da história restaurada.": "Native story progression restored.",
        "SALVAR FORMAÇÃO": "SAVE PARTY",
        "Cores salvas. O co-op ativo recarrega sem reiniciar.": "Colors saved. Active co-op reloads them without restarting.",
        "Cores aplicadas. O co-op ativo atualiza sem reiniciar.": "Colors applied. Active co-op updates without restarting.",
        "Cores da party": "Party colors",
        "IDENTIDADE VISUAL NO JOGO": "VISUAL IDENTITY IN GAME",
        "POR PERSONAGEM": "BY CHARACTER",
        "POR JOGADOR": "BY PLAYER",
        "A cor pertence ao personagem, mesmo quando ele muda de jogador.": "The color belongs to the character, even when they change players.",
        "A cor pertence a P1, P2 ou P3 e acompanha o jogador nas trocas.": "The color belongs to P1, P2, or P3 and follows the player when switching.",
        "PRINCIPAIS": "MAIN",
        "CONVIDADOS": "GUESTS",
        "SALVAR CORES": "SAVE COLORS",
        "CORES APLICADAS AUTOMATICAMENTE": "COLORS APPLY AUTOMATICALLY",
        "A PALETA ACOMPANHA TODA A INTERFACE DO JOGADOR": "THE PALETTE FOLLOWS THE PLAYER'S ENTIRE INTERFACE",
        "LINHA DE ALVO": "TARGET LINE",
        "SELECIONAR": "SELECT",
        "SELECIONAR  ›": "SELECT  ›",
        "ESCOLHA UMA COR": "CHOOSE A COLOR",
        "ATUAL": "CURRENT",
        "CANCELAR": "CANCEL",
        "Clique em um cartão para trocar a paleta · as escolhas do modo inativo são preservadas": "Click a card to change its palette · inactive-mode choices are preserved",
        "NENHUM CONTROLE": "NO CONTROLLER",
        "nenhum controle": "no controller",
        "Pressione uma tecla. Botão direito cancela.": "Press a key. Right-click to cancel.",
        "Atalho removido. Salve para aplicar.": "Binding removed. Save to apply.",
        "Atalho removido e aplicado ao co-op.": "Binding removed and applied to co-op.",
        "Controles salvos. O co-op ativo recarrega sem reiniciar.": "Controls saved. Active co-op reloads them without restarting.",
        "Controles aplicados. O co-op ativo atualiza sem reiniciar.": "Controls applied. Active co-op updates without restarting.",
        "Pressione Restaurar novamente para confirmar.": "Press Restore again to confirm.",
        "Padrões carregados. Salve para aplicar.": "Defaults loaded. Save to apply.",
        "Padrões restaurados e aplicados ao co-op.": "Defaults restored and applied to co-op.",
        "Controles de cada jogador": "Controls for each player",
        "MAPEAMENTO INTEGRADO": "INTEGRATED MAPPING",
        "CONTROLE ATRIBUÍDO": "ASSIGNED CONTROLLER",
        "ATRIBUIR": "ASSIGN",
        "USAR": "USE",
        "TROCAR": "CHANGE",
        "MOVER": "MOVE",
        "EM USO": "IN USE",
        "REMOVER": "REMOVE",
        "NENHUM": "NONE",
        "SEM CONTROLE": "NO CONTROLLER",
        "2 · TECLADO + MOUSE · 1 DISPOSITIVO": "2 · KEYBOARD + MOUSE · 1 DEVICE",
        "2 · TECLADO + MOUSE": "2 · KEYBOARD + MOUSE",
        "SEM DONO": "NO OWNER",
        "MOVER TRANSFERE AMBOS": "MOVE TRANSFERS BOTH",
        "NÃO ATRIBUÍDO": "NOT ASSIGNED",
        "AÇÃO": "ACTION",
        "2 · AÇÕES E ATALHOS (OPCIONAL)": "2 · ACTIONS AND BINDINGS (OPTIONAL)",
        "3 · AÇÕES E ATALHOS (OPCIONAL)": "3 · ACTIONS AND BINDINGS (OPTIONAL)",
        "SALVAR E APLICAR": "SAVE AND APPLY",
        "ALTERAÇÕES APLICADAS AO TERMINAR CADA CAPTURA": "CHANGES APPLY AFTER EACH CAPTURE",
        "RESTAURAR PADRÕES": "RESTORE DEFAULTS",
        "P1 · CAMINHOS DE ENTRADA TRADUZIDOS PARA O JOGO": "P1 · INPUT PATHS TRANSLATED INTO THE GAME",
        "NAVEGAÇÃO": "NAVIGATION",
        "Direcional e ombros": "D-pad and shoulder buttons",
        "Menu de batalha": "Battle menu",
        "AÇÕES": "ACTIONS",
        "Confirmar e cancelar": "Confirm and cancel",
        "Interações e portas": "Interactions and doors",
        "Interações": "Interactions",
        "MENU NATIVO": "NATIVE MENU",
        "Party e Licenças": "Party and Licenses",
        "Atalho dedicado": "Dedicated shortcut",
        "DETECTANDO CONTROLE": "DETECTING CONTROLLER",
        "CAPTURANDO ATALHO": "CAPTURING BINDING",
        "Pressione START no controle desejado": "Press START on the controller you want",
        "Pressione START apenas no controle físico desejado": "Press START only on the physical controller you want",
        "XInput e SDL duplicados serão unificados automaticamente.": "Duplicate XInput and SDL entries will be unified automatically.",
        "Pressione uma tecla": "Press a key",
        "Solte entradas que já estavam pressionadas antes de tentar.": "Release any inputs that were already held before trying.",
        "AGUARDANDO ENTRADA…": "WAITING FOR INPUT…",
        "BOTÃO DIREITO DO MOUSE: CANCELAR": "RIGHT MOUSE BUTTON: CANCEL",
        "Captura cancelada.": "Capture canceled.",
        "Essa tecla não pode ser usada.": "This key cannot be used.",
        "Ajuste salvo.": "Setting saved.",
        "Ajustes do configurador": "Configurator settings",
        "COMPORTAMENTO E SOM": "BEHAVIOR AND SOUND",
        "Idioma": "Language",
        "IDIOMA": "LANGUAGE",
        "Idioma da ferramenta": "Launcher language",
        "Escolha o idioma da ferramenta.": "Choose the launcher language.",
        "Escolha o idioma usado em toda a interface.": "Choose the language used throughout the interface.",
        "Idioma alterado e salvo.": "Language changed and saved.",
        "Inglês": "English",
        "INGLÊS": "ENGLISH",
        "Sons do menu": "Menu sounds",
        "Reproduz sons ao navegar ou confirmar opções do configurador.": "Play sounds when navigating or confirming configurator options.",
        "Som do Battle Menu": "Battle Menu sound",
        "Ative ou silencie o menu de cada jogador.": "Enable or mute each player's battle menu.",
        "Som do Battle Menu salvo.": "Battle Menu sound saved.",
        "Contador de dano": "Damage meter",
        "Ordena o dano causado pela party ativa.": "Ranks damage dealt by the active party.",
        "Sessão por personagem.": "Per-character session.",
        "Salvo por personagem até Zerar.": "Saved per character until Reset.",
        "Por personagem até Zerar.": "Per character until Reset.",
        "Modo do contador de dano salvo.": "Damage meter mode saved.",
        "ZERAR": "RESET",
        "CONFIRMAR": "CONFIRM",
        "Pressione Zerar novamente para iniciar uma nova sessão de dano.": "Press Reset again to start a new damage session.",
        "Nova sessão de dano iniciada. O jogo ativo atualiza sem reiniciar.": "New damage session started. An active game updates without restarting.",
        "EFETIVO": "EFFECTIVE",
        "BRUTO": "RAW",
        "ÁREA": "AREA",
        "Volume da interface": "Interface volume",
        "Minimizar quando estiver pronto": "Minimize when ready",
        "O configurador é minimizado quando a sessão fica ativa.": "Minimize the configurator when the co-op session becomes active.",
        "ATIVO": "ON",
        "DESL.": "OFF",
        "COMO USAR": "HOW TO USE",
        "Monte a formação e escolha se o elenco completo começa liberado.": "Build the party and choose whether the full roster starts unlocked.",
        "Escolha em Cores se o elenco completo começa liberado.": "Choose under Colors whether the full roster starts unlocked.",
        "Escolha em Iniciar se o elenco completo começa liberado.": "Choose under Start whether the full roster starts unlocked.",
        "Defina cores e controles; tudo é salvo no próprio mod.": "Set colors and controls; everything is saved in the mod itself.",
        "Abra pela Steam ou pelo perfil modificado do MO2.": "Start through Steam or the modded MO2 profile.",
        "Abra pela Steam ou pelo FFXII (Modded) do MO2.": "Start through Steam or MO2's FFXII (Modded) entry.",
        "INICIE O JOGO PELA STEAM OU PELO FFXII (MODDED) DO MO2": "START THE GAME THROUGH STEAM OR MO2'S FFXII (MODDED) ENTRY",
        "CONFIGURAÇÃO SALVA": "CONFIGURATION SAVED",
        "Configurações prontas. O co-op inicia automaticamente com o jogo.": "Settings ready. Co-op starts automatically with the game.",
        "MOUSE: selecionar e configurar · BOTÃO DIREITO: cancelar ou limpar": "MOUSE: select and configure · RIGHT-CLICK: cancel or clear",
        "O configurador já está aberto. Procure o ícone do co-op perto do relógio.": "The configurator is already open. Look for the co-op icon in the system tray.",
        "Nenhuma captura ativa.": "No capture is active.",
        "Mais de um controle respondeu. Solte todos e pressione START somente no controle desejado.": "More than one controller responded. Release them all, then press START only on the controller you want.",
        "O Jogador 1 usa apenas um controle XInput.": "Player 1 can only use an XInput controller.",
        "O Jogador 1 usa apenas um controle.": "Player 1 can only use a controller.",
        "Teclado e mouse já estão sem jogador.": "Keyboard and mouse are already unassigned.",
        "Teclado e mouse ficaram sem jogador.": "Keyboard and mouse are now unassigned.",
        "Controle não suportado.": "Unsupported controller.",
        "Comando alterado. Salve para aplicar.": "Binding changed. Save to apply.",
        "Comando alterado.": "Binding changed.",
        "Tecla não suportada.": "Unsupported key.",
        "Jogo encontrado. Preparando memória, controles e interface…": "Game found. Preparing memory, controls, and interface…",
        "Sessão encontrada. Confirmando controles e interface…": "Session found. Confirming controls and interface…",
        "Co-op ativo. Pode carregar ou continuar o jogo.": "Co-op is active. You can load or continue the game.",
        "Núcleo conectado. Carregue ou continue um save para concluir a validação.": "Core connected. Load or continue a save to complete validation.",
        "Save carregado. Confirmando controles, HUD e cores…": "Save loaded. Confirming controls, HUD, and colors…",
        "Co-op ativo. Boa viagem por Ivalice.": "Co-op active. Safe travels through Ivalice.",
        "Sessão co-op encontrada. Conectando controles e interface…": "Co-op session found. Connecting controls and interface…",
        "Co-op já ativo. Launcher reconectado à sessão.": "Co-op is already active. Launcher reattached to the session.",
        "Co-op ativo. Launcher reconectado à sessão.": "Co-op active. Launcher reattached to the session.",
        "A sessão co-op foi encerrada. Pronto para ligar novamente.": "The co-op session ended. Ready to start it again.",
        "Jogo encerrado. Pronto para uma nova sessão.": "Game closed. Ready for a new session.",
        "Sessão co-op encerrada.": "Co-op session ended.",
        "A pasta de FINAL FANTASY XII não foi encontrada. No MO2, defina Start in como a pasta x64 real do jogo.": "The FINAL FANTASY XII folder was not found. In MO2, set Start in to the game's real x64 folder.",
        "A instalação do jogo está incompleta: x64/FFXII_TZA.exe não foi encontrado.": "The game installation is incomplete: x64/FFXII_TZA.exe was not found.",
        "x64\\hook.ini não está configurado para carregar a pasta modules. Reinstale o FF12 External File Loader.": "x64\\hook.ini is not configured to load the modules folder. Reinstall FF12 External File Loader.",
        "O caminho mods\\deploy\\ff12data precisa ser a primeira prioridade do External File Loader. Corrija a configuração do loader antes de iniciar o co-op.": "mods\\deploy\\ff12data must be the External File Loader's first priority. Fix the loader configuration before starting co-op.",
        "O configurador não consegue gravar as configurações. Confira as permissões da pasta do mod.": "The configurator cannot save its settings. Check the mod folder permissions.",
        "Pressione uma tecla ou botão. Botão direito cancela.": "Press a key or button. Right-click to cancel.",
        "sem detalhes": "no details",
    }
)


_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
    (
        re.compile(r"^A alteração não pôde ser aplicada: (.+)$"),
        lambda match: f"The change could not be applied: {match.group(1)}",
    ),
    (re.compile(r"^VERSÃO (.+)$"), lambda match: f"VERSION {match.group(1)}"),
    (re.compile(r"^Membro (\d+)$"), lambda match: f"Member {match.group(1)}"),
    (re.compile(r"^JOGADOR (\d+)$"), lambda match: f"PLAYER {match.group(1)}"),
    (
        re.compile(r"^JOGADOR (\d+) · (.+)$"),
        lambda match: f"PLAYER {match.group(1)} · {match.group(2)}",
    ),
    (re.compile(r"^Controle (\d+)$"), lambda match: f"Controller {match.group(1)}"),
    (
        re.compile(r"^Controle XInput (\d+)$"),
        lambda match: f"XInput Controller {match.group(1)}",
    ),
    (
        re.compile(r"^1 · CONTROLE DO (P\d+)$"),
        lambda match: f"1 · {match.group(1)} CONTROLLER",
    ),
    (
        re.compile(r"^DONO: (P\d+)$"),
        lambda match: f"OWNER: {match.group(1)}",
    ),
    (
        re.compile(r"^DONO ÚNICO: (P\d+)$"),
        lambda match: f"SOLE OWNER: {match.group(1)}",
    ),
    (
        re.compile(r"^Teclado e mouse atribuídos a (P\d+)\.$"),
        lambda match: f"Keyboard and mouse assigned to {match.group(1)}.",
    ),
    (
        re.compile(r"^Teclado e mouse transferidos de (P\d+) para (P\d+)\.$"),
        lambda match: (
            f"Keyboard and mouse transferred from {match.group(1)} to "
            f"{match.group(2)}."
        ),
    ),
    (
        re.compile(r"^Teclado e mouse já pertencem a (P\d+)\.$"),
        lambda match: f"Keyboard and mouse already belong to {match.group(1)}.",
    ),
    (
        re.compile(r"^Teclado e mouse pertencem a (P\d+)\.$"),
        lambda match: f"Keyboard and mouse belong to {match.group(1)}.",
    ),
    (
        re.compile(
            r"^Teclado e mouse pertencem a (P\d+)\. Transfira o dispositivo "
            r"compartilhado antes de usar uma tecla em (P\d+)\.$"
        ),
        lambda match: (
            f"Keyboard and mouse belong to {match.group(1)}. Transfer the shared "
            f"device before using a key for {match.group(2)}."
        ),
    ),
    (
        re.compile(
            r"^Teclado e mouse não estão atribuídos\. Atribua o dispositivo "
            r"compartilhado a (P\d+) antes de usar uma tecla\.$"
        ),
        lambda match: (
            "Keyboard and mouse are unassigned. Assign the shared device to "
            f"{match.group(1)} before using a key."
        ),
    ),
    (
        re.compile(
            r"^Configuração de controles atualizada: teclado e mouse agora "
            r"pertencem a (P\d+|nenhum jogador)\. Migração salva\.$"
        ),
        lambda match: (
            "Controls configuration updated: keyboard and mouse now belong to "
            + (
                match.group(1)
                if match.group(1).startswith("P")
                else "no player"
            )
            + ". Migration saved."
        ),
    ),
    (
        re.compile(
            r"^Teclado e mouse estavam atribuídos a mais de um jogador\. "
            r"Mantido em (P\d+|nenhum jogador); removido de (.+)\. Correção salva\.$"
        ),
        lambda match: (
            "Keyboard and mouse were assigned to more than one player. Kept for "
            f"{match.group(1)}; removed from {match.group(2)}. Repair saved."
        ),
    ),
    (
        re.compile(
            r"^O arquivo de controles usa a versão (\d+), mas este configurador "
            r"suporta até a versão (\d+)\. O arquivo foi preservado sem alterações; "
            r"atualize o mod para editá-lo\.$"
        ),
        lambda match: (
            f"The controls file uses version {match.group(1)}, but this configurator "
            f"supports up to version {match.group(2)}. The file was preserved "
            "unchanged; update the mod to edit it."
        ),
    ),
    (
        re.compile(r"^Pressione START no controle de (P\d+)\.$"),
        lambda match: f"Press START on {match.group(1)}'s controller.",
    ),
    (
        re.compile(r"^(P\d+) agora está sem controle\. Salve para aplicar\.$"),
        lambda match: (f"{match.group(1)} now has no controller. Save to apply."),
    ),
    (
        re.compile(r"^(P\d+) agora está sem controle\. Aplicado ao co-op\.$"),
        lambda match: (f"{match.group(1)} now has no controller. Applied to co-op."),
    ),
    (
        re.compile(
            r"^(.+\.) XInput e SDL foram reconhecidos como o mesmo controle físico\.$"
        ),
        lambda match: (
            f"{translate(match.group(1), 'en')} XInput and SDL were recognized "
            "as the same physical controller."
        ),
    ),
    (
        re.compile(r"^Não foi possível zerar a sessão de dano: (.+)$"),
        lambda match: f"Could not reset the damage session: {match.group(1)}",
    ),
    (
        re.compile(r"^Use o Controle (\d+), atribuído a (P\d+)\.$"),
        lambda match: (
            f"Use Controller {match.group(1)}, assigned to {match.group(2)}."
        ),
    ),
    (
        re.compile(
            r"^Controle (\d+) atribuído a (P\d+); o controle anterior de "
            r"(P\d+) foi movido para (P\d+)\.$"
        ),
        lambda match: (
            f"Controller {match.group(1)} assigned to {match.group(2)}; "
            f"{match.group(3)}'s previous controller was moved to "
            f"{match.group(4)}."
        ),
    ),
    (
        re.compile(r"^Controle (\d+) atribuído a (P\d+)\.$"),
        lambda match: (f"Controller {match.group(1)} assigned to {match.group(2)}."),
    ),
    (
        re.compile(r"^Use o controle atribuído a (P\d+)\.$"),
        lambda match: f"Use the controller assigned to {match.group(1)}.",
    ),
    (
        re.compile(
            r"^(.+?) atribuído a (P\d+); o controle anterior de (P\d+) "
            r"foi movido para (P\d+)\.$"
        ),
        lambda match: (
            f"{match.group(1)} assigned to {match.group(2)}; "
            f"{match.group(3)}'s previous controller was moved to "
            f"{match.group(4)}."
        ),
    ),
    (
        re.compile(r"^(.+?) atribuído a (P\d+)\.$"),
        lambda match: f"{match.group(1)} assigned to {match.group(2)}.",
    ),
    (
        re.compile(r"^(.+) Aplicado ao co-op\.$"),
        lambda match: f"{translate(match.group(1), 'en')} Applied to co-op.",
    ),
    (
        re.compile(r"^(.+?)  ·  ATALHO (\d+)$"),
        lambda match: (
            f"{translate(match.group(1), 'en')}  ·  BINDING {match.group(2)}"
        ),
    ),
    (
        re.compile(r"^(.+?)  ·  XINPUT (\d+)$"),
        lambda match: (
            f"{translate(match.group(1), 'en')}  ·  XINPUT {match.group(2)}"
        ),
    ),
    (
        re.compile(r"^(.+?)  ·  DIRECTINPUT / SDL$"),
        lambda match: f"{match.group(1)}  ·  DIRECTINPUT / SDL",
    ),
    (
        re.compile(r"^Não foi possível salvar a opção de elenco: (.+)$"),
        lambda match: f"Could not save the roster option: {match.group(1)}",
    ),
    (
        re.compile(r"^Não foi possível salvar a formação: (.+)$"),
        lambda match: f"Could not save the party: {match.group(1)}",
    ),
    (
        re.compile(r"^Não foi possível salvar as cores: (.+)$"),
        lambda match: f"Could not save the colors: {match.group(1)}",
    ),
    (
        re.compile(r"^Não foi possível salvar antes de iniciar: (.+)$"),
        lambda match: f"Could not save before starting: {match.group(1)}",
    ),
    (
        re.compile(r"^Não foi possível salvar: (.+)$"),
        lambda match: f"Could not save: {match.group(1)}",
    ),
    (
        re.compile(r"^Controle capturado, mas não foi possível aplicar: (.+)$"),
        lambda match: f"Controller captured, but it could not be applied: {match.group(1)}",
    ),
    (
        re.compile(r"^co-op encerrou com código (-?\d+): (.+)$"),
        lambda match: (
            f"co-op exited with code {match.group(1)}: "
            f"{translate(match.group(2), 'en')}"
        ),
    ),
    (
        re.compile(
            r"^Microsoft Visual C\+\+ 2015-2022 Redistributable \(x64\) "
            r"ausente\. Instale-o e tente novamente\. DLLs ausentes: (.+)$"
        ),
        lambda match: (
            "Microsoft Visual C++ 2015-2022 Redistributable (x64) is missing. "
            f"Install it and try again. Missing DLLs: {match.group(1)}"
        ),
    ),
    (
        re.compile(
            r"^x64\\hook\.ini usa carregamento explicito, mas nao lista todos "
            r"os modulos do Ivalice Together: (.+)$"
        ),
        lambda match: (
            "x64\\hook.ini uses explicit loading but does not list every "
            f"Ivalice Together module: {match.group(1)}"
        ),
    ),
    (
        re.compile(r"^A instalação do jogo está incompleta\. Arquivos ausentes: (.+)$"),
        lambda match: f"The game installation is incomplete. Missing files: {match.group(1)}",
    ),
    (
        re.compile(
            r"^Esta versão de FFXII_TZA\.exe não é compatível com o runtime "
            r"atual do Ivalice Together \(SHA-256 observado: ([0-9A-F]+)\)\.$"
        ),
        lambda match: (
            "This FFXII_TZA.exe version is not compatible with the current "
            f"Ivalice Together runtime (observed SHA-256: {match.group(1)})."
        ),
    ),
    (
        re.compile(
            r"^nao foi possivel reservar a memoria do co-op durante a abertura "
            r"do jogo(?:: (.+))?$"
        ),
        lambda match: (
            "could not reserve co-op memory while opening the game"
            + (f": {match.group(1)}" if match.group(1) else "")
        ),
    ),
    (
        re.compile(r"^helper do co-op encerrou: (.+)$"),
        lambda match: f"co-op helper exited: {match.group(1)}",
    ),
    (
        re.compile(r"^co-op nao confirmou controles/HUD/temas: (.+)$"),
        lambda match: f"co-op did not confirm controls/HUD/themes: {match.group(1)}",
    ),
    (
        re.compile(r"^co-op nao confirmou o contrato final: (.+)$"),
        lambda match: f"co-op did not confirm its final runtime contract: {match.group(1)}",
    ),
    (
        re.compile(r"^a pilha de loaders carregada não é utilizável: (.+)$"),
        lambda match: f"loaded loader stack is unusable: {match.group(1)}",
    ),
)


def normalize_language(language: object) -> str:
    value = str(language or "").strip().replace("_", "-").casefold()
    if value in {"pt", "pt-br", "português", "portugues", "portuguese"}:
        return "pt"
    if value in {"en", "en-us", "en-gb", "english"}:
        return "en"
    return DEFAULT_LANGUAGE


def translate(text: str, language: object = DEFAULT_LANGUAGE) -> str:
    if normalize_language(language) == "pt":
        return text
    translated = _ENGLISH.get(text)
    if translated is not None:
        return translated

    marker = "  •"
    if text.endswith(marker):
        base = text[: -len(marker)]
        translated_base = translate(base, "en")
        if translated_base != base:
            return translated_base + marker

    for pattern, replacement in _PATTERNS:
        match = pattern.fullmatch(text)
        if match is not None:
            return replacement(match)  # type: ignore[operator]
    return text


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "normalize_language",
    "translate",
]
