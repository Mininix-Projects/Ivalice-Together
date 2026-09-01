from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterable, Protocol

from headless_menu_model import (
    MenuEvent,
    MenuMode,
    P2MenuModel,
    SubmitRequest,
    TargetCandidate,
    TargetGroup,
)
from headless_menu_presenter import HeadlessMenuPresenter


class InputCommand(Enum):
    TOGGLE = auto()
    UP = auto()
    DOWN = auto()
    UP_REPEAT = auto()
    DOWN_REPEAT = auto()
    PAGE_UP = auto()
    PAGE_DOWN = auto()
    CONFIRM = auto()
    BACK = auto()
    FOES = auto()
    NEUTRALS = auto()
    PARTY = auto()
    CYCLE_TARGET_GROUP = auto()
    CYCLE_TARGET_GROUP_PREVIOUS = auto()


class TargetProvider(Protocol):
    def __call__(self, model: P2MenuModel) -> Iterable[TargetCandidate]: ...


class Submitter(Protocol):
    def __call__(self, request: SubmitRequest) -> int: ...


@dataclass(frozen=True)
class FrontendResult:
    event: MenuEvent
    native_result: int | None = None


class HeadlessFrontendController:
    def __init__(
        self,
        model: P2MenuModel,
        *,
        target_provider: TargetProvider,
        submitter: Submitter,
    ) -> None:
        self.model = model
        self.presenter = HeadlessMenuPresenter(model)
        self.target_provider = target_provider
        self.submitter = submitter

    def refresh_targets(self) -> None:
        if self.model.mode is MenuMode.TARGET:
            self.model.set_candidates(self.target_provider(self.model))

    def dispatch(self, command: InputCommand) -> FrontendResult:
        if command is InputCommand.TOGGLE:
            event = self.model.toggle()
            if event.kind == "opened":
                self.presenter.reset_visual_state()
            return FrontendResult(event)
        if command is InputCommand.UP:
            return FrontendResult(self.model.navigate(-1))
        if command is InputCommand.DOWN:
            return FrontendResult(self.model.navigate(1))
        if command is InputCommand.UP_REPEAT:
            return FrontendResult(self.model.navigate(-1, wrap=False))
        if command is InputCommand.DOWN_REPEAT:
            return FrontendResult(self.model.navigate(1, wrap=False))
        if command is InputCommand.PAGE_UP:
            if self.model.mode is not MenuMode.LIST:
                return FrontendResult(MenuEvent("ignored"))
            return FrontendResult(
                self.model.navigate_page(
                    -1,
                    self.presenter.visible_row_limit(self.model.page.key),
                )
            )
        if command is InputCommand.PAGE_DOWN:
            if self.model.mode is not MenuMode.LIST:
                return FrontendResult(MenuEvent("ignored"))
            return FrontendResult(
                self.model.navigate_page(
                    1,
                    self.presenter.visible_row_limit(self.model.page.key),
                )
            )
        if command is InputCommand.BACK:
            return FrontendResult(self.model.back())
        if command is InputCommand.FOES:
            event = self.model.switch_target_group(TargetGroup.FOES)
            self.refresh_targets()
            return FrontendResult(event)
        if command is InputCommand.NEUTRALS:
            event = self.model.switch_target_group(TargetGroup.NEUTRALS)
            self.refresh_targets()
            return FrontendResult(event)
        if command is InputCommand.PARTY:
            event = self.model.switch_target_group(TargetGroup.PARTY)
            self.refresh_targets()
            return FrontendResult(event)
        if command is InputCommand.CYCLE_TARGET_GROUP:
            event = self.model.cycle_target_group()
            self.refresh_targets()
            return FrontendResult(event)
        if command is InputCommand.CYCLE_TARGET_GROUP_PREVIOUS:
            event = self.model.cycle_target_group(-1)
            self.refresh_targets()
            return FrontendResult(event)
        if command is not InputCommand.CONFIRM:
            return FrontendResult(MenuEvent("ignored"))

        event = self.model.confirm()
        if event.kind == "target_opened":
            self.refresh_targets()
            return FrontendResult(event)
        if event.kind == "submit" and event.request:
            return FrontendResult(event, self.submitter(event.request))
        return FrontendResult(event)


__all__ = [
    "FrontendResult",
    "HeadlessFrontendController",
    "InputCommand",
    "Submitter",
    "TargetProvider",
]
