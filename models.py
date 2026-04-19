from enum import Enum

class GameState(Enum):
    WAITING_FOR_PLAYER = 1
    CHOOSING_TYPE = 2
    CHOOSING_OPTION = 3
    WAITING_FOR_RESPONSE = 4
    ENDED = 5