export function requestError(failure: unknown) {
  if (failure instanceof Error && failure.name === "AbortError") {
    return "Ответ не получен вовремя. Проверьте планы перед повторной отправкой: событие могло сохраниться.";
  }
  if (failure instanceof Error && !(failure instanceof TypeError) && !(failure instanceof SyntaxError)) {
    return failure.message;
  }
  return "Ответ сервера не получен. Проверьте подключение и планы перед повторной отправкой: событие могло сохраниться.";
}
