from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import sys
import time

import coop_launcher as base
import launcher_core as core
import pygame
from PIL import Image, ImageChops, ImageDraw


DEEP_INK = (4, 10, 16)
SURFACE = (11, 25, 36)
SURFACE_RAISED = (16, 34, 47)
HAIRLINE = (67, 91, 107)
SOFT_TEXT = (151, 166, 176)
CYAN = (94, 190, 218)
GOLD = (218, 174, 82)
GOLD_PALE = (255, 226, 163)
GREEN = (101, 218, 171)
REDESIGN_AA_SCALE = 6
BRAND_LOGO_MAX_SIZE = (205, 64)
INTERNAL_RUNTIME_STEM = "ivalicetogetherruntime"


def is_internal_runtime_binary(
    executable: str | Path | None = None,
    *,
    frozen: bool | None = None,
) -> bool:
    """Identify the private onedir worker when it is opened without a role."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    program = Path(sys.executable) if executable is None else Path(executable)
    return bool(is_frozen) and program.stem.casefold() == INTERNAL_RUNTIME_STEM


class RedesignFonts(base.Fonts):
    def __init__(self) -> None:
        display = pygame.font.match_font("garamond") or pygame.font.match_font(
            "georgia"
        )
        text = pygame.font.match_font("segoeui") or pygame.font.match_font("arial")
        semibold = (
            pygame.font.match_font("segoeuisemibold")
            or pygame.font.match_font("segoeuibold")
            or text
        )
        self._specs: dict[int, tuple[str | None, int]] = {}
        self._scaled: dict[tuple[int, int], pygame.font.Font] = {}
        self.hero = self._font(display, 51)
        self.title = self._font(display, 39)
        self.subtitle = self._font(display, 23)
        self.body = self._font(text, 18)
        self.body_small = self._font(text, 16)
        self.label = self._font(semibold, 15)
        self.micro = self._font(semibold, 12)
        self.button = self._font(display, 25)
        self.binding = self._font(semibold, 13)
        self.card_name = self._font(display, 23)


class RedesignLauncherApp(base.LauncherApp):
    def __init__(self, *, preview: bool = False) -> None:
        super().__init__(preview=preview)
        self.fonts = RedesignFonts()
        self.brand_logo = self._load_brand_logo()
        self.panel_cache.clear()
        self.box_cache.clear()
        self.text_cache.clear()

    @staticmethod
    def _load_brand_logo() -> pygame.Surface:
        source_path = core.bundled_asset("ivalice_together_logo.png")
        with Image.open(source_path) as image:
            rgba = image.convert("RGBA")
            content_bounds = rgba.getchannel("A").getbbox()
            if content_bounds is None:
                raise ValueError(f"logo sem pixels visiveis: {source_path}")
            cropped = rgba.crop(content_bounds)
            scale = min(
                BRAND_LOGO_MAX_SIZE[0] / cropped.width,
                BRAND_LOGO_MAX_SIZE[1] / cropped.height,
            )
            size = (
                max(1, round(cropped.width * scale)),
                max(1, round(cropped.height * scale)),
            )
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            return RedesignLauncherApp._pygame_surface(cropped.resize(size, resampling))

    @staticmethod
    def _make_static_layers() -> tuple[
        pygame.Surface,
        pygame.Surface,
        pygame.Surface,
    ]:
        veil = pygame.Surface(base.LOGICAL_SIZE, pygame.SRCALPHA)
        veil.fill((0, 3, 7, 34))

        wash = pygame.Surface(base.LOGICAL_SIZE, pygame.SRCALPHA)
        pygame.draw.rect(wash, (3, 13, 22, 239), pygame.Rect(25, 24, 230, 762))
        pygame.draw.line(wash, (229, 186, 94, 150), (50, 24), (216, 24), 1)
        pygame.draw.line(wash, (72, 115, 139, 70), (255, 55), (255, 756), 1)

        for x in range(250, 1110):
            progress = (x - 250) / 860.0
            alpha = round(154 * (1.0 - progress) ** 1.7)
            pygame.draw.line(wash, (1, 9, 16, alpha), (x, 0), (x, 809))

        capture_shade = pygame.Surface(base.LOGICAL_SIZE, pygame.SRCALPHA)
        capture_shade.fill((1, 7, 12, 218))
        return veil, wash, capture_shade

    def localize(self, text: str) -> str:
        copy = {
            "Uma configuração. Jogue do seu jeito.": {
                "pt": "Configure uma vez. Jogue de onde preferir.",
                "en": "Set it once. Launch it your way.",
            },
            "Elenco, identidade visual e controles permanecem salvos no mod.": {
                "pt": "Party, cores e controles ficam salvos no próprio mod.",
                "en": "Party, colors, and controls are saved with the mod.",
            },
            "AUTOSTART VIA MODULE LOADER": {
                "pt": "CO-OP AUTOMÁTICO ATIVO",
                "en": "CO-OP AUTO-START ENABLED",
            },
            "Uma paleta. Todos os sinais.": {
                "pt": "Uma cor em toda a interface de combate.",
                "en": "One color across the entire combat interface.",
            },
            "Dano da party ativa.": {
                "pt": "Dano da party ativa.",
                "en": "Active-party damage.",
            },
            "ENTRADA DO JOGADOR 1": {
                "pt": "ENTRADA DO JOGADOR 1",
                "en": "PLAYER 1 INPUT",
            },
            "Tudo fica salvo no mod. Inicie pela Steam ou pelo FFXII (Modded) do MO2.": {
                "pt": "Tudo fica salvo no mod. Inicie pela Steam ou pelo FFXII (Modded) do MO2.",
                "en": "Everything stays with the mod. Start through Steam or MO2's FFXII (Modded) entry.",
            },
        }
        localized = copy.get(text)
        if localized is not None:
            return localized[self.language]
        return super().localize(text)

    def set_page(self, page: str) -> None:
        if page == self.page:
            if page == "controls":
                self.resume_controller_discovery()
            self.selected_key = f"nav:{page}"
            self.reset_pointer()
            return
        self.page = page
        if page == "controls":
            self.resume_controller_discovery()
        else:
            self.suspend_controller_discovery()
        self.selected_key = f"nav:{page}"
        self.reset_pointer()
        self.audio.play("open")

    def prepare_navigation_visuals(self, now: float) -> None:
        """Warm tab-specific drawing caches behind the first visible frame."""

        current_page = self.page
        current_selection = self.selected_key
        pointer_y = self.pointer_y
        pointer_trail = list(self.pointer_trail)

        for page, _label in base.PAGES:
            self.page = page
            self.selected_key = f"nav:{page}"
            self.scene = None
            self.scene_dirty = True
            self.render(now=now)
            pygame.event.pump()

        self.page = current_page
        self.selected_key = current_selection
        self.pointer_y = pointer_y
        self.pointer_trail = pointer_trail
        self.scene = None
        self.scene_dirty = True
        self.render(now=now)

    def draw_background(self, now: float) -> None:
        self.canvas.blit(self.background, (0, 0))
        self.canvas.blit(self.background_veil, (0, 0))
        self.canvas.blit(self.background_wash, (0, 0))
        for index, (x, y, speed, phase) in enumerate(self.motes):
            if index % 3:
                continue
            pulse = 0.5 + 0.5 * math.sin(now * speed * 1.5 + phase * 7.0)
            color = GOLD if index % 2 else CYAN
            pygame.draw.circle(
                self.canvas,
                (*color, round(24 + pulse * 28)),
                (x, y),
                1,
            )
        self._redesign_backdrop = self.canvas.copy()

    def _clear_region(self, rect: pygame.Rect) -> None:
        self.discard_deferred_text(rect)
        self.canvas.blit(self._redesign_backdrop, rect, rect)

    def _shade_region(self, rect: pygame.Rect, alpha: int = 222) -> None:
        shade = pygame.Surface(rect.size, pygame.SRCALPHA)
        for x in range(rect.width):
            progress = x / max(1, rect.width - 1)
            value = round(alpha * (0.42 + progress * 0.58))
            pygame.draw.line(shade, (1, 7, 12, value), (x, 0), (x, rect.height))
        self.canvas.blit(shade, rect)

    def draw_home(self) -> None:
        super().draw_home()
        region = pygame.Rect(base.CONTENT_X, 534, base.CONTENT_W, 172)
        self._clear_region(region)
        self._shade_region(region, 242)
        self.draw_aa_line(
            self.canvas,
            GOLD,
            (region.x, region.y + 14),
            (region.x + 34, region.y + 14),
            2,
        )
        self.draw_text(
            self.canvas,
            self.fonts.subtitle,
            "Uma configuração. Jogue do seu jeito.",
            (region.x, region.y + 26),
            base.WHITE,
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            "Elenco, identidade visual e controles permanecem salvos no mod.",
            (region.x, region.y + 59),
            SOFT_TEXT,
            shadow=False,
        )
        details = (
            ("ELENCO", "Party e Gambits"),
            ("IDENTIDADE", "HUD, menus e alvos"),
            ("CONTROLES", "XInput e DirectInput"),
        )
        column_width = region.width // 3
        for index, (label, detail) in enumerate(details):
            x = region.x + index * column_width
            if index:
                self.draw_aa_line(
                    self.canvas,
                    (*HAIRLINE, 90),
                    (x, region.y + 91),
                    (x, region.y + 141),
                )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                label,
                (x + (16 if index else 0), region.y + 94),
                GOLD_PALE,
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.body_small,
                detail,
                (x + (16 if index else 0), region.y + 117),
                SOFT_TEXT,
                shadow=False,
            )

    def draw_colors(self) -> None:
        super().draw_colors()
        region = pygame.Rect(base.CONTENT_X, 640, base.CONTENT_W, 103)
        self._clear_region(region)
        self._shade_region(region, 238)
        self.draw_aa_line(
            self.canvas,
            GOLD,
            (region.x, region.y + 13),
            (region.x + 34, region.y + 13),
            2,
        )
        self.draw_text(
            self.canvas,
            self.fonts.subtitle,
            "Uma paleta. Todos os sinais.",
            (region.x, region.y + 24),
            base.WHITE,
        )
        labels = ("BATTLE MENU", "HUD", "TARGET LINE", "TARGET INFO")
        x = region.x
        for index, label in enumerate(labels):
            color = CYAN if index % 2 == 0 else GOLD_PALE
            self.draw_aa_line(
                self.canvas,
                color,
                (x, region.y + 75),
                (x + 20, region.y + 75),
                2,
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                label,
                (x + 29, region.y + 69),
                SOFT_TEXT,
                shadow=False,
            )
            x += 135

    def draw_controls(self) -> None:
        super().draw_controls()
        if self.control_player != "p1":
            return
        guide = pygame.Rect(base.CONTENT_X, 521, 728, 162)
        self._clear_region(guide)
        self.draw_panel(self.canvas, guide, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "ENTRADA DO JOGADOR 1",
            (guide.x + 18, guide.y + 16),
            GOLD_PALE,
            shadow=False,
        )
        routes = (
            ("NAVEGAÇÃO", "Direcional e ombros", "Battle Menu"),
            ("AÇÕES", "Confirmar e cancelar", "Interações"),
            ("MENU NATIVO", "Party e Licenças", "Atalho dedicado"),
        )
        width = 232
        for index, (label, source, destination) in enumerate(routes):
            x = guide.x + 18 + index * width
            if index:
                self.draw_aa_line(
                    self.canvas,
                    (*HAIRLINE, 100),
                    (x - 14, guide.y + 46),
                    (x - 14, guide.bottom - 19),
                )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                label,
                (x, guide.y + 50),
                CYAN,
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.body_small,
                source,
                (x, guide.y + 76),
                base.WHITE,
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                destination,
                (x, guide.y + 105),
                SOFT_TEXT,
                shadow=False,
            )

    def draw_settings(self) -> None:
        super().draw_settings()
        region = pygame.Rect(base.CONTENT_X, 654, base.CONTENT_W, 92)
        self._clear_region(region)
        self._shade_region(region, 242)
        self.draw_aa_line(
            self.canvas,
            GOLD,
            (region.x, region.y + 17),
            (region.x + 34, region.y + 17),
            2,
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            self.fit_text(
                self.fonts.body_small,
                "Tudo fica salvo no mod. Inicie pela Steam ou pelo FFXII (Modded) do MO2.",
                region.width,
            ),
            (region.x, region.y + 33),
            SOFT_TEXT,
            shadow=False,
        )

    @staticmethod
    def _rgba(color: tuple[int, ...]) -> tuple[int, int, int, int]:
        if len(color) == 4:
            return tuple(int(channel) for channel in color)
        return tuple(int(channel) for channel in color[:3]) + (255,)

    @staticmethod
    def _pygame_surface(image: Image.Image) -> pygame.Surface:
        return pygame.image.fromstring(
            image.tobytes(), image.size, "RGBA"
        ).convert_alpha()

    def _render_panel_surface(
        self,
        size: tuple[int, int],
        *,
        selected: bool,
        strong: bool,
        accent: tuple[int, int, int] | None,
    ) -> pygame.Surface:
        scale = REDESIGN_AA_SCALE
        high_size = (max(1, size[0] * scale), max(1, size[1] * scale))
        edge = scale // 2
        radius = 8 * scale
        bounds = (
            edge,
            edge,
            high_size[0] - edge - 1,
            high_size[1] - edge - 1,
        )

        high = Image.new("RGBA", high_size, (0, 0, 0, 0))
        shadow = Image.new("RGBA", high_size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle(
            (
                bounds[0] + 2 * scale,
                bounds[1] + 3 * scale,
                bounds[2] - 2 * scale,
                bounds[3],
            ),
            radius=radius,
            fill=(0, 0, 0, 145),
        )
        high.alpha_composite(shadow)

        top = SURFACE_RAISED if strong else SURFACE
        bottom = DEEP_INK
        if selected:
            top = (23, 46, 60)
            bottom = (8, 22, 32)
        alpha_top = 244 if strong or selected else 222
        alpha_bottom = 248 if strong or selected else 232
        gradient = Image.new("RGBA", high_size, (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        for y in range(high_size[1]):
            mix = y / max(1, high_size[1] - 1)
            color = tuple(
                round(top[channel] * (1.0 - mix) + bottom[channel] * mix)
                for channel in range(3)
            ) + (round(alpha_top * (1.0 - mix) + alpha_bottom * mix),)
            gradient_draw.line((0, y, high_size[0], y), fill=color)
        mask = Image.new("L", high_size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=radius, fill=255)
        gradient.putalpha(ImageChops.multiply(gradient.getchannel("A"), mask))
        high.alpha_composite(gradient)

        border = accent or (GOLD if selected else HAIRLINE)
        border_alpha = 214 if selected or accent else (150 if strong else 105)
        draw = ImageDraw.Draw(high)
        draw.rounded_rectangle(
            bounds,
            radius=radius,
            outline=(*border, border_alpha),
            width=(2 if selected else 1) * scale,
        )
        if selected:
            draw.rounded_rectangle(
                (
                    edge,
                    10 * scale,
                    edge + 3 * scale,
                    high_size[1] - 10 * scale,
                ),
                radius=2 * scale,
                fill=(*GOLD_PALE, 245),
            )
        elif accent:
            draw.line(
                (
                    12 * scale,
                    edge + scale // 2,
                    high_size[0] - 12 * scale,
                    edge + scale // 2,
                ),
                fill=(*accent, 210),
                width=scale,
            )

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        return self._pygame_surface(high.resize(size, resampling))

    def draw_aa_box(
        self,
        rect: pygame.Rect,
        fill: tuple[int, ...],
        border: tuple[int, ...],
        *,
        width: int = 1,
        radius: int = 3,
    ) -> None:
        """Draw every compact control through the same anti-aliased path."""

        cache_key = (
            "redesign_box_v2",
            rect.size,
            tuple(fill),
            tuple(border),
            int(width),
            int(radius),
        )
        cached = self.box_cache.get(cache_key)
        if cached is None:
            scale = REDESIGN_AA_SCALE
            size = (max(1, rect.width * scale), max(1, rect.height * scale))
            high = Image.new("RGBA", size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(high)
            edge = scale // 2
            bounds = (
                edge,
                edge,
                max(edge, size[0] - edge - 1),
                max(edge, size[1] - edge - 1),
            )
            effective_radius = max(3, int(radius)) * scale
            draw.rounded_rectangle(
                bounds,
                radius=effective_radius,
                fill=self._rgba(fill),
                outline=self._rgba(border),
                width=max(scale, int(width) * scale),
            )
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            cached = self._pygame_surface(high.resize(rect.size, resampling))
            self.box_cache[cache_key] = cached
        self.canvas.blit(cached, rect)

    def draw_theme_swatch(self, rect: pygame.Rect, value: str) -> None:
        theme = base.menu_themes.get_theme(value)
        cache_key = ("redesign_swatch", rect.size, value)
        cached = self.box_cache.get(cache_key)
        if cached is None:
            scale = REDESIGN_AA_SCALE
            size = (rect.width * scale, rect.height * scale)
            edge = scale // 2
            bounds = (edge, edge, size[0] - edge - 1, size[1] - edge - 1)
            radius = 5 * scale

            gradient = Image.new("RGBA", size, (0, 0, 0, 0))
            gradient_draw = ImageDraw.Draw(gradient)
            for x in range(size[0]):
                mix = x / max(1, size[0] - 1)
                eased = mix * mix * (3.0 - 2.0 * mix)
                color = tuple(
                    round(
                        theme.panel_dark[channel] * (1.0 - eased)
                        + theme.accent[channel] * eased
                    )
                    for channel in range(3)
                )
                gradient_draw.line((x, 0, x, size[1]), fill=(*color, 244))
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=radius, fill=255)
            gradient.putalpha(ImageChops.multiply(gradient.getchannel("A"), mask))

            draw = ImageDraw.Draw(gradient)
            draw.rounded_rectangle(
                bounds,
                radius=radius,
                outline=(*theme.highlight, 245),
                width=scale,
            )
            draw.line(
                (
                    8 * scale,
                    edge + scale,
                    size[0] - 8 * scale,
                    edge + scale,
                ),
                fill=(*theme.highlight, 150),
                width=scale,
            )
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            cached = self._pygame_surface(gradient.resize(rect.size, resampling))
            self.box_cache[cache_key] = cached
        self.canvas.blit(cached, rect)

    def draw_theme_card(
        self,
        profile: base.menu_themes.CharacterThemeProfile,
        rect: pygame.Rect,
    ) -> None:
        super().draw_theme_card(profile, rect)
        value = self.character_theme_draft[profile.member_id]
        self.draw_theme_swatch(
            pygame.Rect(rect.x + 82, rect.y + 55, rect.width - 98, 26),
            value,
        )

    def draw_player_theme_card(self, player: str, rect: pygame.Rect) -> None:
        super().draw_player_theme_card(player, rect)
        value = self.player_theme_draft[player]
        self.draw_theme_swatch(
            pygame.Rect(rect.x + 24, rect.bottom - 64, rect.width - 48, 27),
            value,
        )

    def draw_damage_meter_row(self, y: int) -> None:
        """Keep the damage-meter copy in its own column beside its controls."""

        rect = pygame.Rect(base.CONTENT_X, y, base.CONTENT_W, 78)
        enabled = bool(self.config.get("damage_meter_enabled", False))
        mode = str(self.config.get("damage_meter_mode", "area"))
        effective = bool(self.config.get("damage_meter_effective", False))
        enabled_badge = pygame.Rect(rect.right - 438, rect.y + 22, 66, 34)
        area_badge = pygame.Rect(rect.right - 364, rect.y + 22, 62, 34)
        session_badge = pygame.Rect(rect.right - 294, rect.y + 22, 76, 34)
        effective_badge = pygame.Rect(rect.right - 210, rect.y + 22, 84, 34)
        reset_badge = pygame.Rect(rect.right - 118, rect.y + 22, 100, 34)
        enabled_selected = self.register(
            "settings:damage_meter:enabled",
            enabled_badge,
            lambda: self.toggle_setting("damage_meter_enabled"),
        )
        area_selected = self.register(
            "settings:damage_meter:area",
            area_badge,
            lambda: self.set_damage_meter_mode("area"),
        )
        session_selected = self.register(
            "settings:damage_meter:indefinite",
            session_badge,
            lambda: self.set_damage_meter_mode("indefinite"),
        )
        effective_selected = self.register(
            "settings:damage_meter:effective",
            effective_badge,
            lambda: self.toggle_setting("damage_meter_effective"),
        )
        reset_selected = self.register(
            "settings:damage_meter:reset",
            reset_badge,
            self.reset_damage_meter_session,
        )
        self.draw_panel(
            self.canvas,
            rect,
            selected=(
                enabled_selected
                or area_selected
                or session_selected
                or effective_selected
                or reset_selected
            ),
            strong=True,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "Contador de dano",
            (rect.x + 20, rect.y + 16),
            base.WHITE,
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            "Por personagem até Zerar.",
            (rect.x + 20, rect.y + 44),
            SOFT_TEXT,
            shadow=False,
        )
        self.draw_badge(
            enabled_badge,
            "ATIVO" if enabled else "DESL.",
            active=enabled,
            selected=enabled_selected,
            color=GREEN if enabled else SOFT_TEXT,
        )
        self.draw_badge(
            area_badge,
            "\u00c1REA",
            active=mode == "area",
            selected=area_selected,
            color=GOLD_PALE,
        )
        self.draw_badge(
            session_badge,
            "SESS\u00c3O",
            active=mode == "indefinite",
            selected=session_selected,
            color=GOLD_PALE,
        )
        self.draw_badge(
            effective_badge,
            "EFETIVO" if effective else "BRUTO",
            active=effective,
            selected=effective_selected,
            color=GREEN if effective else SOFT_TEXT,
        )
        reset_pending = time.monotonic() <= self.damage_reset_confirm_until
        self.draw_badge(
            reset_badge,
            "CONFIRMAR" if reset_pending else "ZERAR",
            active=reset_pending,
            selected=reset_selected,
            color=base.RED if reset_pending else GOLD_PALE,
        )

    def draw_panel(
        self,
        surface: pygame.Surface,
        rect: pygame.Rect,
        *,
        selected: bool = False,
        strong: bool = False,
        accent: tuple[int, int, int] | None = None,
    ) -> None:
        cache_key = (
            "redesign_panel",
            rect.size,
            bool(selected),
            bool(strong),
            tuple(accent) if accent else None,
        )
        cached = self.panel_cache.get(cache_key)
        if cached is None:
            cached = self._render_panel_surface(
                rect.size,
                selected=selected,
                strong=strong,
                accent=accent,
            )
            self.panel_cache[cache_key] = cached
        surface.blit(cached, rect)

    def draw_badge(
        self,
        rect: pygame.Rect,
        label: str,
        *,
        active: bool = False,
        selected: bool = False,
        color: tuple[int, int, int] = GOLD_PALE,
    ) -> None:
        fill = (
            (*color, 40 if active else 18) if active or selected else (7, 17, 25, 180)
        )
        border = (*color, 225) if active or selected else (*HAIRLINE, 125)
        self.draw_aa_box(
            rect,
            fill,
            border,
            width=2 if selected else 1,
            radius=min(10, rect.height // 2),
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            label,
            rect.center,
            color if active or selected else SOFT_TEXT,
            anchor="center",
            shadow=False,
        )

    def draw_header(
        self,
        title: str,
        kicker: str,
        *,
        width: int = base.CONTENT_W,
    ) -> None:
        number = {
            "home": "01",
            "colors": "02",
            "controls": "03",
            "settings": "04",
        }.get(self.page, "--")
        self.draw_aa_line(
            self.canvas, GOLD, (base.CONTENT_X, 85), (base.CONTENT_X + 24, 85), 2
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            kicker,
            (base.CONTENT_X + 34, 79),
            GOLD_PALE,
            shadow=False,
        )
        right = base.CONTENT_X + width
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"{number}  /  04",
            (right, 79),
            CYAN,
            anchor="topright",
            shadow=False,
        )
        has_title = bool(title.strip())
        if has_title:
            self.draw_text(
                self.canvas,
                self.fonts.title,
                title,
                (base.CONTENT_X - 2, 99),
                base.WHITE,
            )
        line_y = 153 if has_title else 116
        self.draw_aa_line(
            self.canvas,
            (*GOLD, 225),
            (base.CONTENT_X, line_y),
            (base.CONTENT_X + 74, line_y),
            2,
        )
        self.draw_aa_line(
            self.canvas, (*HAIRLINE, 95), (base.CONTENT_X + 74, line_y), (right, line_y)
        )

    def draw_nav(self) -> None:
        logo_rect = self.brand_logo.get_rect(midtop=(140, 39))
        self.canvas.blit(self.brand_logo, logo_rect)
        self.draw_aa_line(self.canvas, (*GOLD, 185), (52, 119), (219, 119))
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "LOCAL CO-OP  /  2–3P",
            (52, 134),
            (104, 135, 153),
            shadow=False,
        )

        for index, (page, label) in enumerate(base.PAGES):
            rect = pygame.Rect(47, 183 + index * 62, 195, 48)
            selected = self.register(
                f"nav:{page}", rect, lambda value=page: self.set_page(value)
            )
            active = page == self.page
            if active or selected:
                fill = (18, 38, 51, 220 if selected else 178)
                border = GOLD if selected else (70, 112, 135)
                self.draw_aa_box(rect, fill, border, width=1, radius=7)
            if active:
                self.draw_aa_line(
                    self.canvas,
                    GOLD_PALE,
                    (rect.x + 1, rect.y + 9),
                    (rect.x + 1, rect.bottom - 9),
                    3,
                )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                f"{index + 1:02d}",
                (rect.x + 18, rect.centery),
                GOLD_PALE if active else (94, 122, 139),
                anchor="midleft",
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.label,
                label,
                (rect.x + 58, rect.centery),
                base.WHITE if active or selected else SOFT_TEXT,
                anchor="midleft",
                shadow=False,
            )

        state = getattr(self.launcher, "state", "idle")
        if state == "ready":
            color, label = GREEN, "CO-OP ATIVO"
        elif self.game_open:
            color, label = GOLD_PALE, "CO-OP AUTOMÁTICO"
        else:
            color, label = (102, 137, 156), "AGUARDANDO JOGO"
        self.draw_aa_circle(self.canvas, color, (55, 711), 4)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            label,
            (68, 705),
            color,
            shadow=False,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"IVALICE TOGETHER  /  v{core.LAUNCHER_VERSION}",
            (52, 735),
            (72, 103, 121),
            shadow=False,
        )

    def draw_status_bar(self) -> None:
        if base.time.monotonic() > self.status_until and self.launcher.state not in (
            "preparing",
            "ready",
            "error",
        ):
            message = (
                "MOUSE: selecionar e configurar · BOTÃO DIREITO: cancelar ou limpar"
            )
            color = (105, 128, 143)
        else:
            message = (
                self.launcher.last_message
                if self.launcher.state
                in ("preparing", "ready", "error")
                else self.status_message
            )
            color = {
                "ok": GREEN,
                "error": base.RED,
                "warning": GOLD_PALE,
                "capture": CYAN,
            }.get(
                "error" if self.launcher.state == "error" else self.status_kind,
                SOFT_TEXT,
            )
        y = 774
        self.draw_aa_line(
            self.canvas,
            (*HAIRLINE, 100),
            (base.CONTENT_X, y - 9),
            (base.CONTENT_X + base.CONTENT_W, y - 9),
        )
        self.draw_aa_circle(self.canvas, color, (base.CONTENT_X + 5, y + 2), 3)
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            self.fit_text(self.fonts.body_small, message, base.CONTENT_W - 28),
            (base.CONTENT_X + 18, y + 2),
            color,
            anchor="midleft",
            shadow=False,
        )


def main() -> int:
    if is_internal_runtime_binary():
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None,
                "This is an internal runtime component. It starts automatically "
                "with FFXII. Open IvaliceTogetherConfig.exe to configure the mod.",
                "Ivalice Together",
                0x40,
            )
        return 0
    instance = core.acquire_launcher_instance()
    if instance is None:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None,
                base.already_open_message(),
                "Ivalice Together",
                0x40,
            )
        return 0
    try:
        return RedesignLauncherApp().run()
    finally:
        core.release_launcher_instance(instance)


if __name__ == "__main__":
    raise SystemExit(main())
