"""
ДОПОЛНИТЕЛЬНЫЕ ОБРАБОТЧИКИ ДЛЯ MULTI-ACCOUNT API KEYS SYSTEM

Этот файл содержит новые обработчики для управления API ключами
с поддержкой account_priority (PRIMARY/SECONDARY/TERTIARY).

ВАЖНО: Этот код нужно добавить в конец callback.py перед обработчиком неизвестных callback
"""

# ============================================================================
# ОБРАБОТЧИКИ ДЛЯ ДОБАВЛЕНИЯ/ОБНОВЛЕНИЯ API КЛЮЧЕЙ С PRIORITY
# ============================================================================

@router.callback_query(F.data.startswith("add_api_key_priority_") | F.data.startswith("update_api_key_priority_"))
async def callback_add_update_api_key_with_priority(callback: CallbackQuery, state: FSMContext):
    """
    Начало процесса добавления/обновления API ключа с указанным priority (Multi-Account Support)

    Обрабатывает callback_data:
    - add_api_key_priority_1 / add_api_key_priority_2 / add_api_key_priority_3
    - update_api_key_priority_1 / update_api_key_priority_2 / update_api_key_priority_3
    """
    user_id = callback.from_user.id

    try:
        # Парсим callback_data для получения action и priority
        if callback.data.startswith("add_api_key_priority_"):
            action = "add"
            priority = int(callback.data.replace("add_api_key_priority_", ""))
        else:  # update_api_key_priority_
            action = "update"
            priority = int(callback.data.replace("update_api_key_priority_", ""))

        # Валидация priority
        if priority not in [1, 2, 3]:
            await callback.answer("❌ Неверный приоритет ключа", show_alert=True)
            return

        # Определяем имя для отображения
        priority_names = {
            1: "PRIMARY (Bot 1)",
            2: "SECONDARY (Bot 2)",
            3: "TERTIARY (Bot 3)"
        }
        priority_name = priority_names[priority]
        action_text = "Обновление" if action == "update" else "Добавление"

        # Сохраняем priority в состояние
        await state.set_state(UserStates.AWAITING_API_KEY)
        await state.update_data(
            menu_message_id=callback.message.message_id,
            api_key_priority=priority,
            api_key_action=action
        )

        text = (
            f"🔑 <b>{action_text} {priority_name} API ключа Bybit</b>\n\n"
            f"Шаг 1 из 2: Введите <b>API Key</b>\n\n"
            f"⚠️ <b>ВАЖНО:</b>\n"
            f"• Сообщение с ключом будет автоматически удалено после ввода\n"
            f"• Ключ будет зашифрован перед сохранением в базе данных\n"
            f"• API ключ должен иметь права на торговлю (Trade)\n\n"
            f"💡 <b>Для Multi-Account режима:</b>\n"
            f"   У каждого ключа должны быть права на отдельный суб-аккаунт Bybit\n\n"
            f"Введите ваш API Key для {priority_name}:"
        )

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_back_keyboard("api_keys")
        )
        await callback.answer()

        log_info(user_id, f"Пользователь начал процесс {action} API ключа с priority={priority}", module_name='callback')

    except Exception as e:
        log_error(user_id, f"Ошибка начала добавления/обновления API ключа: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)


# ПРИМЕЧАНИЕ: process_api_key_input и process_api_secret_input уже существуют,
# но их нужно ПЕРЕДЕЛАТЬ для поддержки priority. Вот переделанные версии:

# Переделанный обработчик для PROCESS_API_SECRET_INPUT
async def process_api_secret_input_MULTI_ACCOUNT(message: Message, state: FSMContext):
    """
    Обработка ввода API Secret с немедленным удалением и сохранением в БД (Multi-Account Support)

    ВАЖНО: Эта функция ЗАМЕНЯЕТ старый process_api_secret_input в callback.py
    """
    user_id = message.from_user.id

    try:
        api_secret = message.text.strip()

        # Валидация API секрета (базовая проверка формата)
        if len(api_secret) < 10:
            await message.answer("❌ API Secret слишком короткий. Попробуйте еще раз.")
            await message.delete()
            return

        # Немедленно удаляем сообщение пользователя
        await message.delete()

        # Получаем сохраненные данные из состояния
        state_data = await state.get_data()
        api_key = state_data.get("api_key")
        menu_message_id = state_data.get("menu_message_id")
        priority = state_data.get("api_key_priority", 1)  # По умолчанию PRIMARY
        action = state_data.get("api_key_action", "add")

        if not api_key:
            await message.answer("❌ Ошибка: API Key не найден. Начните процесс заново.")
            await state.clear()
            return

        # Сохраняем ключи в базу данных с указанным priority
        success = await db_manager.save_api_keys(
            user_id=user_id,
            exchange="bybit",
            api_key=api_key,
            secret_key=api_secret,
            account_priority=priority  # КЛЮЧЕВОЕ ИЗМЕНЕНИЕ - передаем priority
        )

        if success:
            # Показываем короткую версию ключа для подтверждения
            api_key_short = api_key[:4] + '...' + api_key[-4:]

            priority_names = {
                1: "PRIMARY (Bot 1)",
                2: "SECONDARY (Bot 2)",
                3: "TERTIARY (Bot 3)"
            }
            priority_name = priority_names[priority]

            # Получаем ОБНОВЛЕННОЕ количество ключей для правильного отображения клавиатуры
            all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
            api_keys_count = len(all_keys)

            action_text = "обновлены" if action == "update" else "сохранены"

            text = (
                f"✅ <b>{priority_name} API ключи успешно {action_text}!</b>\n\n"
                f"🔑 <b>API Key:</b> <code>{api_key_short}</code>\n"
                f"🔢 <b>Приоритет:</b> {priority}\n\n"
                f"🔒 Ваши ключи зашифрованы и надежно хранятся в базе данных.\n"
                f"🗑️ Все сообщения с ключами были удалены из чата.\n\n"
            )

            # Добавляем информацию о Multi-Account режиме
            if api_keys_count == 1:
                text += (
                    f"💡 <b>Текущий режим:</b> Обычный (1 бот)\n"
                    f"   Добавьте еще 2 ключа для активации Multi-Account режима!"
                )
            elif api_keys_count == 2:
                text += (
                    f"💡 <b>Текущий режим:</b> Переходный (2 бота)\n"
                    f"   Добавьте еще 1 ключ для полноценного Multi-Account режима!"
                )
            elif api_keys_count >= 3:
                text += (
                    f"🎉 <b>Multi-Account режим АКТИВЕН!</b>\n"
                    f"   Система автоматически управляет 3 ботами с ротацией!"
                )

            log_info(user_id, f"API ключи успешно {action_text} для пользователя {user_id}, priority={priority}, всего ключей={api_keys_count}", module_name='callback')
        else:
            api_keys_count = 0  # При ошибке показываем меню для 0 ключей
            text = (
                f"❌ <b>Ошибка сохранения ключей</b>\n\n"
                f"Не удалось сохранить API ключи в базу данных. "
                f"Попробуйте еще раз или обратитесь в поддержку."
            )
            log_error(user_id, f"Ошибка сохранения API ключей с priority={priority} в БД", module_name='callback')

        from ..keyboards.inline import get_api_keys_keyboard
        await bot_manager.bot.edit_message_text(
            chat_id=user_id,
            message_id=menu_message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=get_api_keys_keyboard(api_keys_count=api_keys_count)  # ИСПРАВЛЕНО
        )

        # Очищаем состояние
        await state.clear()

    except Exception as e:
        log_error(user_id, f"Ошибка обработки API Secret: {e}", module_name='callback')
        await message.answer("❌ Произошла ошибка при сохранении ключей.")
        await message.delete()
        await state.clear()


# ============================================================================
# ОБРАБОТЧИКИ ДЛЯ УДАЛЕНИЯ API КЛЮЧЕЙ С PRIORITY
# ============================================================================

@router.callback_query(F.data.startswith("delete_api_key_priority_"))
async def callback_delete_api_key_with_priority(callback: CallbackQuery, state: FSMContext):
    """
    Подтверждение удаления конкретного API ключа по priority (Multi-Account Support)

    Обрабатывает callback_data:
    - delete_api_key_priority_1 (PRIMARY)
    - delete_api_key_priority_2 (SECONDARY)
    - delete_api_key_priority_3 (TERTIARY)
    """
    user_id = callback.from_user.id

    try:
        # Парсим priority из callback_data
        priority = int(callback.data.replace("delete_api_key_priority_", ""))

        # Валидация priority
        if priority not in [1, 2, 3]:
            await callback.answer("❌ Неверный приоритет ключа", show_alert=True)
            return

        # Определяем имя для отображения
        priority_names = {
            1: "PRIMARY (Bot 1)",
            2: "SECONDARY (Bot 2)",
            3: "TERTIARY (Bot 3)"
        }
        priority_name = priority_names[priority]

        # Проверяем, существует ли ключ с таким priority
        all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
        key_exists = any(key['priority'] == priority for key in all_keys)

        if not key_exists:
            await callback.answer(f"❌ {priority_name} ключ не найден", show_alert=True)
            return

        # Получаем информацию о ключе для отображения
        target_key = next(key for key in all_keys if key['priority'] == priority)
        api_key_short = target_key['api_key'][:4] + '...' + target_key['api_key'][-4:]

        text = (
            f"⚠️ <b>Подтверждение удаления {priority_name} API ключа</b>\n\n"
            f"🔑 <b>API Key:</b> <code>{api_key_short}</code>\n"
            f"🔢 <b>Приоритет:</b> {priority}\n\n"
            f"Вы уверены, что хотите удалить этот ключ?\n\n"
        )

        # Добавляем предупреждение в зависимости от количества ключей
        if len(all_keys) == 3:
            text += (
                f"⚠️ <b>ВНИМАНИЕ:</b> После удаления этого ключа у вас останется 2 ключа.\n"
                f"   Multi-Account режим будет частично деактивирован."
            )
        elif len(all_keys) == 2:
            text += (
                f"⚠️ <b>ВНИМАНИЕ:</b> После удаления этого ключа у вас останется 1 ключ.\n"
                f"   Система перейдет в обычный режим (1 бот)."
            )
        elif len(all_keys) == 1:
            text += (
                f"🚨 <b>ВНИМАНИЕ:</b> Это ваш единственный ключ!\n"
                f"   После удаления вы не сможете использовать автоматическую торговлю."
            )

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard(f"delete_api_key_priority_{priority}")
        )
        await callback.answer()

    except Exception as e:
        log_error(user_id, f"Ошибка отображения подтверждения удаления API ключа: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("confirm_delete_api_key_priority_"))
async def callback_confirm_delete_api_key_with_priority(callback: CallbackQuery, state: FSMContext):
    """
    Выполнение удаления конкретного API ключа по priority (Multi-Account Support)
    """
    user_id = callback.from_user.id

    try:
        # Парсим priority из callback_data
        priority = int(callback.data.replace("confirm_delete_api_key_priority_", ""))

        # Валидация priority
        if priority not in [1, 2, 3]:
            await callback.answer("❌ Неверный приоритет ключа", show_alert=True)
            return

        # Удаляем конкретный ключ через деактивацию записи в БД
        query = """
            UPDATE user_api_keys
            SET is_active = FALSE, updated_at = NOW()
            WHERE user_id = $1 AND exchange = $2 AND account_priority = $3
        """

        async with db_manager.get_connection() as conn:
            result = await conn.execute(query, user_id, "bybit", priority)

        # Проверяем, был ли удален ключ
        if result == "UPDATE 0":
            await callback.answer("❌ Ключ не найден", show_alert=True)
            return

        # Получаем ОБНОВЛЕННОЕ количество ключей
        all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
        api_keys_count = len(all_keys)

        priority_names = {
            1: "PRIMARY (Bot 1)",
            2: "SECONDARY (Bot 2)",
            3: "TERTIARY (Bot 3)"
        }
        priority_name = priority_names[priority]

        text = (
            f"✅ <b>{priority_name} API ключ успешно удален</b>\n\n"
            f"🔢 Удален ключ с приоритетом: {priority}\n"
            f"📊 Осталось ключей: {api_keys_count}/3\n\n"
        )

        # Добавляем информацию о текущем режиме
        if api_keys_count == 0:
            text += (
                f"⚠️ У вас больше нет API ключей.\n"
                f"   Добавьте новые ключи для возобновления торговли."
            )
        elif api_keys_count == 1:
            text += (
                f"💡 Система переключена в обычный режим (1 бот).\n"
                f"   Добавьте 2 дополнительных ключа для Multi-Account режима."
            )
        elif api_keys_count == 2:
            text += (
                f"💡 Multi-Account режим частично активен (2 бота).\n"
                f"   Добавьте еще 1 ключ для полного Multi-Account режима."
            )

        log_info(user_id, f"API ключ с priority={priority} удален для пользователя {user_id}, осталось {api_keys_count} ключей", module_name='callback')

        from ..keyboards.inline import get_api_keys_keyboard
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_api_keys_keyboard(api_keys_count=api_keys_count)  # ИСПРАВЛЕНО
        )
        await callback.answer("Ключ удален", show_alert=False)

    except Exception as e:
        log_error(user_id, f"Ошибка удаления API ключа с priority: {e}", module_name='callback')
        await callback.answer("❌ Ошибка при удалении ключа", show_alert=True)


# ============================================================================
# ОБРАБОТЧИК ДЛЯ КНОПКИ "NOOP" (ЗАГЛУШКА ДЛЯ ИНФОРМАЦИОННОЙ КНОПКИ)
# ============================================================================

@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery, state: FSMContext):
    """
    Заглушка для информационной кнопки "Multi-Account режим АКТИВЕН"

    Эта кнопка не выполняет никаких действий, просто показывает уведомление
    """
    await callback.answer(
        "🎉 Multi-Account режим активен! Система автоматически управляет 3 ботами.",
        show_alert=True
    )