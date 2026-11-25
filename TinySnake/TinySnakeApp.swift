import SwiftUI
import Combine

@main
struct TinySnakeApp: App {
    var body: some Scene { WindowGroup { ContentView() } }
}

struct ContentView: View {
    @State private var game = Game()
    @State private var playing = false
    @Environment(\.scenePhase) private var scenePhase
    private let timer = Timer.publish(every: 0.17, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(spacing: 20) {
            Text("SNAKE").font(.largeTitle.bold().monospaced())
            Text("Score \(game.score)").font(.title2.monospacedDigit())
            Canvas { context, size in
                let unit = size.width / CGFloat(Game.size)
                for cell in game.snake {
                    let rect = CGRect(x: CGFloat(cell.x) * unit + 1, y: CGFloat(cell.y) * unit + 1,
                                      width: unit - 2, height: unit - 2)
                    context.fill(Path(roundedRect: rect, cornerRadius: 3), with: .color(.green))
                }
                if let food = game.food {
                    let rect = CGRect(x: CGFloat(food.x) * unit + 2, y: CGFloat(food.y) * unit + 2,
                                      width: unit - 4, height: unit - 4)
                    context.fill(Path(ellipseIn: rect), with: .color(.orange))
                }
            }
            .aspectRatio(1, contentMode: .fit)
            .background(Color(white: 0.08), in: RoundedRectangle(cornerRadius: 12))
            .gesture(DragGesture(minimumDistance: 15).onEnded { value in
                let dx = value.translation.width, dy = value.translation.height
                game.turn(abs(dx) > abs(dy) ? (dx > 0 ? .right : .left) : (dy > 0 ? .down : .up))
            })
            .accessibilityLabel("Snake board. Score \(game.score)")
            Text(game.isOver ? (game.food == nil ? "You win!" : "Game over") : "Swipe or use the arrows")
                .font(.headline)
            VStack(spacing: 8) {
                control("arrow.up", "Up", .up)
                HStack(spacing: 32) {
                    control("arrow.left", "Left", .left)
                    control("arrow.down", "Down", .down)
                    control("arrow.right", "Right", .right)
                }
            }
            Button(game.isOver ? "Play again" : (playing ? "Pause" : "Play")) {
                if game.isOver { game = Game() }
                playing.toggle()
            }
            .buttonStyle(.borderedProminent).tint(.green).foregroundStyle(.black)
            .font(.headline).accessibilityIdentifier("play")
        }
        .padding(24).frame(maxWidth: 500).frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black).foregroundStyle(.white).preferredColorScheme(.dark)
        .onReceive(timer) { _ in
            if playing { game.step(); if game.isOver { playing = false } }
        }
        .onChange(of: scenePhase) { phase in if phase != .active { playing = false } }
    }

    private func control(_ icon: String, _ label: String, _ direction: Direction) -> some View {
        Button { game.turn(direction) } label: {
            Image(systemName: icon).font(.title2.bold()).frame(width: 52, height: 44)
        }
        .buttonStyle(.bordered).tint(.green).accessibilityLabel(label)
    }
}
