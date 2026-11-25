"""All texts as plain data, so they are easy to review and edit.

Client texts: each scenario is a list of sentences. A sentence that ends up empty (for
example the warning slot) is dropped. `snippets` are pieces the code picks from, `warnings`
are the only warning sentences a client ever sees. Amounts are never declined, so the same
text works for any number.
"""

from typing import Any

INTERNAL: dict[str, Any] = {
    "reasons": {
        "out_of_gas": "likely out of gas",
        "reverted": "likely reverted, the revert reason is not decoded",
        "out_of_energy": "out of energy",
        "fee_limit_exceeded": "fee limit exceeded",
        "other": "other failure",
        "unknown": "unknown failure",
    },
    "final": "final",
    "not_final": "not final",
    "unknown": "unknown",
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "en": {
        "scenarios": {
            "success_final": [
                "The transaction on the {network} network was executed successfully and is fully "
                "confirmed.",
                "{details}",
                "{warning}",
                "If the recipient does not see the funds, send them this link.",
            ],
            "success_waiting": [
                "The transaction on the {network} network was successful but is not fully "
                "confirmed yet ({confirmations} of {required} network confirmations).",
                "{details}",
                "{warning}",
                "Please open the link again later to see the current number of confirmations.",
            ],
            "pending": [
                "The transaction on the {network} network has been sent but is not included in a "
                "block yet, "
                "so it is not confirmed.",
                "{details}",
                "{warning}",
                "Please open the link again a little later to see its current state.",
            ],
            "failed_out_of_gas": [
                "The transaction on the {network} network was not completed: it most likely "
                "ran out of its gas limit (gas is the network fee, paid in {native}).",
                "{fee_line}",
                "{details}",
                "{warning}",
                "To try again, send the transaction with a higher gas limit (usually this is a "
                "fee setting in the wallet).",
            ],
            "failed_out_of_energy": [
                "The transaction on the {network} network was not completed: the sender's "
                "wallet ran out of energy (a TRON network resource for smart-contract "
                "operations).",
                "{fee_line}",
                "{details}",
                "{warning}",
                "To try again, make sure the sender's wallet has enough {native} for the fee, or "
                "raise the fee limit in the wallet.",
            ],
            "failed_reverted": [
                "The transaction on the {network} network was not completed: it was most likely "
                "rejected by "
                "the smart contract it called (this is called a \u201crevert\u201d), and the exact "
                "reason is not shown in the blockchain data.",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Please contact the service or the wallet this transaction was sent from.",
            ],
            "failed_other": [
                "The transaction on the {network} network was not completed.",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Please contact the service or the wallet this transaction was sent from.",
            ],
            "not_found": [
                "{not_found_line}",
                "Possible reasons: a typo in the hash, a different network, or the transaction "
                "has not been included in a block yet.",
                "{warning}",
                "Please double-check the hash and the network, and look again in a minute.",
            ],
            "invalid_input": [
                "We could not read this as a transaction hash or a link to a transaction.",
                "Please copy the transaction hash (64 characters, it may start with 0x) or the "
                "link to it from Etherscan or Tronscan and send it again.",
            ],
            "invalid_address": [
                "This looks like a wallet address, not a transaction hash.",
                "Please send the hash of the transaction itself (or its link from Etherscan or "
                "Tronscan) instead; you can find it in the transaction history of your wallet or "
                "service.",
            ],
            "provider_error": [
                "We could not get the data about this transaction right now: the service we get "
                "blockchain data from is temporarily unavailable.",
                "Please try again a little later.",
            ],
        },
        "snippets": {
            "link_line": "You can check the transaction yourself: {link}",
            "details_transfer": "Amount: {amount}, recipient: {to}, time: {when}.",
            "details_none": "No coins or tokens were moved, address: {to}, time: {when}.",
            "details_pending_amount": "Amount: {amount}, recipient: {to}.",
            "details_pending_plain": "Recipient address: {to}.",
            "details_failed": "Recipient: {to}, time: {when}.",
            "details_failed_attempt": "Recipient: {to}, time: {when}; it tried to send "
            "{attempted}, which was not executed.",
            "fee_charged": "A network fee of {fee} was still charged.",
            "raw_units": "{amount} (smallest token units)",
            "time_unknown": "unknown",
            "not_found_one": "We could not find this transaction on {checked}.",
            "not_found_many": "We could not find this transaction {checked}.",
            "not_found_item": "on {name}",
            "not_found_join": " or ",
        },
        "warnings": {
            "lookalike_token": "The token in this transaction is named USDT or USDC, but its "
            "contract does not match the official one, so it may be a fake.",
            "legacy_token": "This is an older version of USDC on TRON, so please check whether "
            "the receiving platform still accepts it.",
            "unknown_token": "The token in this transaction is not in our list of known tokens, "
            "so its amount is shown in the smallest token units.",
            "to_is_contract": "The recipient address is a smart contract, and this is often "
            "normal (for example, smart-contract wallets or the deposit addresses of some "
            "services).",
            "network_unconfirmed": "We could not confirm the network from the link, so "
            "{network} was assumed.",
            "network_guessed": "The network was not specified, so we assumed {network}.",
            "zero_value": "This transaction did not move any coins or tokens.",
        },
    },
    "uk": {
        "scenarios": {
            "success_final": [
                "Транзакцію в мережі {network} успішно виконано та повністю підтверджено.",
                "{details}",
                "{warning}",
                "Якщо отримувач не бачить коштів, надішліть йому це посилання.",
            ],
            "success_waiting": [
                "Транзакцію в мережі {network} успішно виконано, але ще не повністю підтверджено "
                "(підтверджень мережі: {confirmations} з {required}).",
                "{details}",
                "{warning}",
                "Будь ласка, відкрийте посилання пізніше, щоб побачити актуальну кількість "
                "підтверджень.",
            ],
            "pending": [
                "Транзакцію в мережі {network} надіслано, але її ще не включено до блоку, "
                "тому вона не підтверджена.",
                "{details}",
                "{warning}",
                "Будь ласка, відкрийте посилання трохи пізніше, щоб побачити її поточний стан.",
            ],
            "failed_out_of_gas": [
                "Транзакцію в мережі {network} не виконано: імовірно, їй не вистачило "
                "ліміту gas (комісії мережі, яка сплачується в {native}).",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Щоб спробувати ще раз, надішліть транзакцію з вищим лімітом gas (зазвичай це "
                "налаштування комісії в гаманці).",
            ],
            "failed_out_of_energy": [
                "Транзакцію в мережі {network} не виконано: на гаманці відправника "
                "закінчилася енергія (ресурс мережі TRON для операцій зі смарт-контрактами).",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Щоб спробувати ще раз, переконайтеся, що на гаманці відправника достатньо "
                "{native} для комісії, або збільште ліміт комісії (fee limit) у гаманці.",
            ],
            "failed_reverted": [
                "Транзакцію в мережі {network} не виконано: імовірно, смарт-контракт, до "
                "якого вона звернулася, її відхилив (це називається \u00abrevert\u00bb), а "
                "точна причина в даних блокчейну не відображається.",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Будь ласка, зверніться до сервісу або гаманця, з якого було надіслано цю "
                "транзакцію.",
            ],
            "failed_other": [
                "Транзакцію в мережі {network} не виконано.",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Будь ласка, зверніться до сервісу або гаманця, з якого було надіслано цю "
                "транзакцію.",
            ],
            "not_found": [
                "{not_found_line}",
                "Можливі причини: помилка в хеші, інша мережа або транзакцію ще не включено "
                "до блоку.",
                "{warning}",
                "Будь ласка, перевірте хеш і мережу та спробуйте ще раз за хвилину.",
            ],
            "invalid_input": [
                "Не вдалося розпізнати це як хеш транзакції або посилання на транзакцію.",
                "Будь ласка, скопіюйте хеш транзакції (64 символи, може починатися з 0x) або "
                "посилання на неї з Etherscan або Tronscan та надішліть ще раз.",
            ],
            "invalid_address": [
                "Це схоже на адресу гаманця, а не на хеш транзакції.",
                "Будь ласка, надішліть замість неї хеш самої транзакції (або посилання на неї "
                "з Etherscan або Tronscan); його можна знайти в історії операцій гаманця чи "
                "сервісу.",
            ],
            "provider_error": [
                "Зараз не вдалося отримати дані про цю транзакцію: сервіс, з якого ми "
                "отримуємо дані блокчейну, тимчасово недоступний.",
                "Будь ласка, спробуйте трохи пізніше.",
            ],
        },
        "snippets": {
            "link_line": "Ви можете самостійно перевірити транзакцію: {link}",
            "details_transfer": "Сума: {amount}, отримувач: {to}, час: {when}.",
            "details_none": "Монети чи токени не переміщувалися, адреса: {to}, час: {when}.",
            "details_pending_amount": "Сума: {amount}, отримувач: {to}.",
            "details_pending_plain": "Адреса отримувача: {to}.",
            "details_failed": "Отримувач: {to}, час: {when}.",
            "details_failed_attempt": "Отримувач: {to}, час: {when}; транзакція намагалася "
            "надіслати {attempted}, але цього не сталося.",
            "fee_charged": "Комісію мережі в розмірі {fee} усе одно було списано.",
            "raw_units": "{amount} (мінімальних одиниць токена)",
            "time_unknown": "невідомий",
            "not_found_one": "Ми не знайшли цю транзакцію в мережі {checked}.",
            "not_found_many": "Ми не знайшли цю транзакцію {checked}.",
            "not_found_item": "ні в мережі {name}",
            "not_found_join": ", ",
        },
        "warnings": {
            "lookalike_token": "Токен у цій транзакції має назву USDT або USDC, але його "
            "контракт не збігається з офіційним, тому це може бути підробка.",
            "legacy_token": "Це старіша версія USDC у мережі TRON, тому, будь ласка, "
            "перевірте, чи приймає її платформа-отримувач.",
            "unknown_token": "Цього токена немає в нашому списку відомих, тому суму показано "
            "в мінімальних одиницях токена.",
            "to_is_contract": "Адреса отримувача — це смарт-контракт, і це часто нормально "
            "(наприклад, смарт-контрактні гаманці або депозитні адреси деяких сервісів).",
            "network_unconfirmed": "Не вдалося підтвердити мережу за посиланням, тому ми "
            "припустили {network}.",
            "network_guessed": "Мережу не було вказано, тому ми припустили {network}.",
            "zero_value": "Ця транзакція не переміщувала жодних монет чи токенів.",
        },
    },
    "ru": {
        "scenarios": {
            "success_final": [
                "Транзакция в сети {network} успешно выполнена и полностью подтверждена.",
                "{details}",
                "{warning}",
                "Если получатель не видит средств, отправьте ему эту ссылку.",
            ],
            "success_waiting": [
                "Транзакция в сети {network} успешно выполнена, но ещё не полностью "
                "подтверждена (подтверждений сети: {confirmations} из {required}).",
                "{details}",
                "{warning}",
                "Пожалуйста, откройте ссылку позже, чтобы увидеть актуальное количество "
                "подтверждений.",
            ],
            "pending": [
                "Транзакция в сети {network} отправлена, но ещё не включена в блок, "
                "поэтому не подтверждена.",
                "{details}",
                "{warning}",
                "Пожалуйста, откройте ссылку чуть позже, чтобы увидеть её текущее состояние.",
            ],
            "failed_out_of_gas": [
                "Транзакция в сети {network} не выполнена: вероятно, ей не хватило лимита "
                "gas (комиссии сети, которая оплачивается в {native}).",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Чтобы попробовать ещё раз, отправьте транзакцию с более высоким лимитом gas "
                "(обычно это настройка комиссии в кошельке).",
            ],
            "failed_out_of_energy": [
                "Транзакция в сети {network} не выполнена: на кошельке отправителя "
                "закончилась энергия (ресурс сети TRON для операций со смарт-контрактами).",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Чтобы попробовать ещё раз, убедитесь, что на кошельке отправителя достаточно "
                "{native} для комиссии, или увеличьте лимит комиссии (fee limit) в кошельке.",
            ],
            "failed_reverted": [
                "Транзакция в сети {network} не выполнена: вероятно, смарт-контракт, к "
                "которому она обратилась, её отклонил (это называется \u00abrevert\u00bb), а "
                "точная причина в данных блокчейна не отображается.",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Пожалуйста, обратитесь в сервис или кошелёк, из которого была отправлена эта "
                "транзакция.",
            ],
            "failed_other": [
                "Транзакция в сети {network} не выполнена.",
                "{fee_line}",
                "{details}",
                "{warning}",
                "Пожалуйста, обратитесь в сервис или кошелёк, из которого была отправлена эта "
                "транзакция.",
            ],
            "not_found": [
                "{not_found_line}",
                "Возможные причины: опечатка в хеше, другая сеть или транзакция ещё не попала "
                "в блок.",
                "{warning}",
                "Пожалуйста, проверьте хеш и сеть и попробуйте ещё раз через минуту.",
            ],
            "invalid_input": [
                "Не удалось распознать это как хеш транзакции или ссылку на транзакцию.",
                "Пожалуйста, скопируйте хеш транзакции (64 символа, может начинаться с 0x) или "
                "ссылку на неё с Etherscan или Tronscan и отправьте ещё раз.",
            ],
            "invalid_address": [
                "Это похоже на адрес кошелька, а не на хеш транзакции.",
                "Пожалуйста, отправьте вместо него хеш самой транзакции (или ссылку на неё с "
                "Etherscan или Tronscan); его можно найти в истории операций кошелька или "
                "сервиса.",
            ],
            "provider_error": [
                "Сейчас не удалось получить данные об этой транзакции: сервис, из которого мы "
                "получаем данные блокчейна, временно недоступен.",
                "Пожалуйста, попробуйте чуть позже.",
            ],
        },
        "snippets": {
            "link_line": "Вы можете самостоятельно проверить транзакцию: {link}",
            "details_transfer": "Сумма: {amount}, получатель: {to}, время: {when}.",
            "details_none": "Монеты и токены не перемещались, адрес: {to}, время: {when}.",
            "details_pending_amount": "Сумма: {amount}, получатель: {to}.",
            "details_pending_plain": "Адрес получателя: {to}.",
            "details_failed": "Получатель: {to}, время: {when}.",
            "details_failed_attempt": "Получатель: {to}, время: {when}; транзакция пыталась "
            "отправить {attempted}, но этого не произошло.",
            "fee_charged": "Комиссия сети в размере {fee} всё равно была списана.",
            "raw_units": "{amount} (минимальных единиц токена)",
            "time_unknown": "неизвестно",
            "not_found_one": "Мы не нашли эту транзакцию в сети {checked}.",
            "not_found_many": "Мы не нашли эту транзакцию {checked}.",
            "not_found_item": "ни в сети {name}",
            "not_found_join": ", ",
        },
        "warnings": {
            "lookalike_token": "Токен в этой транзакции называется USDT или USDC, но его "
            "контракт не совпадает с официальным, поэтому это может быть подделка.",
            "legacy_token": "Это более старая версия USDC в сети TRON, поэтому, пожалуйста, "
            "проверьте, принимает ли её платформа-получатель.",
            "unknown_token": "Этого токена нет в нашем списке известных, поэтому сумма "
            "показана в минимальных единицах токена.",
            "to_is_contract": "Адрес получателя — это смарт-контракт, и это часто нормально "
            "(например, смарт-контрактные кошельки или депозитные адреса некоторых сервисов).",
            "network_unconfirmed": "Не удалось подтвердить сеть по ссылке, поэтому мы "
            "предположили {network}.",
            "network_guessed": "Сеть не была указана, поэтому мы предположили {network}.",
            "zero_value": "Эта транзакция не перемещала ни монет, ни токенов.",
        },
    },
}
