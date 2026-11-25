// Mutations caught: allowing reversal/second turn, missing growth, ignoring walls,
// moving after game-over, or failing to reset the game.
@main
struct GameChecks {
    static func main() {
        var game = Game()
        game.turn(.left)
        game.step()
        precondition(game.snake[0] == Cell(x: 11, y: 10), "Reversal must be ignored")
        game.step(); game.step()
        precondition(game.score == 1 && game.snake.count == 4, "Eating must grow and score")
        precondition(game.food != nil && !game.snake.contains(game.food!), "Food must be unoccupied")
        game.turn(.up); game.turn(.left); game.step()
        precondition(game.snake[0] == Cell(x: 13, y: 9), "Only one turn per tick")
        for _ in 0..<10 { game.step() }
        precondition(game.isOver, "Wall must end game")
        let stopped = game.snake
        game.step()
        precondition(game.snake == stopped, "Game-over must freeze movement")
        game = Game()
        precondition(!game.isOver && game.score == 0 && game.snake.count == 3, "Reset must restore play")
        print("PASS: reversal, growth, food placement, turn buffering, walls, game-over and reset")
    }
}
