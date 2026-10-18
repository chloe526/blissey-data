#ifndef ESP32_CSI_CSI_COMPONENT_H
#define ESP32_CSI_CSI_COMPONENT_H

#include "time_component.h"
#include "math.h"
#include <sstream>
#include <iostream>
#include <iomanip>
#include "freertos/queue.h"
#include <string.h>

char *project_type;

#define CSI_RAW 1
#define CSI_AMPLITUDE 0
#define CSI_PHASE 0

#define CSI_TYPE CSI_RAW

static const size_t MAX_CSI_BYTES = 640;
struct CsiSample {
    wifi_pkt_rx_ctrl_t rx_ctrl;
    uint8_t mac[6];
    uint16_t len;
    bool first_word_invalid;
    int8_t buf[MAX_CSI_BYTES];
    double timestamp;
};
static QueueHandle_t csi_queue;
static volatile unsigned csi_queue_dropped = 0;

void _wifi_csi_cb(void *, wifi_csi_info_t *data) {
    if (!data || !data->buf || !csi_queue) return;
    CsiSample sample = {};
    size_t length = data->len;
#if CONFIG_SHOULD_COLLECT_ONLY_LLTF
    if (length > 128) length = 128;
#endif
    if (!length || length > MAX_CSI_BYTES || length % 2) return;
    sample.rx_ctrl = data->rx_ctrl;
    memcpy(sample.mac, data->mac, sizeof(sample.mac));
    sample.len = length;
    sample.first_word_invalid = data->first_word_invalid;
    memcpy(sample.buf, data->buf, length);
    sample.timestamp = get_steady_clock_timestamp();
    if (xQueueSend(csi_queue, &sample, 0) != pdTRUE) ++csi_queue_dropped;
}

void print_csi_sample(CsiSample *data) {
    std::stringstream ss;

    const CsiSample &d = data[0];
    char mac[20] = {0};
    sprintf(mac, "%02X:%02X:%02X:%02X:%02X:%02X", d.mac[0], d.mac[1], d.mac[2], d.mac[3], d.mac[4], d.mac[5]);

    ss << "CSI_DATA,"
       << project_type << ","
       << mac << ","
       // https://github.com/espressif/esp-idf/blob/9d0ca60398481a44861542638cfdc1949bb6f312/components/esp_wifi/include/esp_wifi_types.h#L314
       << d.rx_ctrl.rssi << ","
       << d.rx_ctrl.rate << ","
       << d.rx_ctrl.sig_mode << ","
       << d.rx_ctrl.mcs << ","
       << d.rx_ctrl.cwb << ","
       << d.rx_ctrl.smoothing << ","
       << d.rx_ctrl.not_sounding << ","
       << d.rx_ctrl.aggregation << ","
       << d.rx_ctrl.stbc << ","
       << d.rx_ctrl.fec_coding << ","
       << d.rx_ctrl.sgi << ","
       << d.rx_ctrl.noise_floor << ","
       << d.rx_ctrl.ampdu_cnt << ","
       << d.rx_ctrl.channel << ","
       << d.rx_ctrl.secondary_channel << ","
       << d.rx_ctrl.timestamp << ","
       << d.rx_ctrl.ant << ","
       << d.rx_ctrl.sig_len << ","
       << d.rx_ctrl.rx_state << ","
       << real_time_set << ","
       << std::fixed << std::setprecision(6) << d.timestamp << ","
       << data->len << ","
       << (data->first_word_invalid ? 1 : 0) << ",[";

    int data_len = data->len;

int8_t *my_ptr;
#if CSI_RAW
    my_ptr = data->buf;
    for (int i = 0; i < data_len; i++) {
        ss << (int) my_ptr[i] << " ";
    }
#endif
#if CSI_AMPLITUDE
    my_ptr = data->buf;
    for (int i = 0; i < data_len / 2; i++) {
        ss << (int) sqrt(pow(my_ptr[i * 2], 2) + pow(my_ptr[(i * 2) + 1], 2)) << " ";
    }
#endif
#if CSI_PHASE
    my_ptr = data->buf;
    for (int i = 0; i < data_len / 2; i++) {
        ss << (int) atan2(my_ptr[i*2], my_ptr[(i*2)+1]) << " ";
    }
#endif
    ss << "]\n";

#ifdef CONFIG_SEND_CSI_TO_SERIAL
    printf("%s", ss.str().c_str());
#endif
}

void csi_writer_task(void *) {
    CsiSample sample;
    unsigned reported_drops = 0;
    for (;;) {
        if (xQueueReceive(csi_queue, &sample, pdMS_TO_TICKS(1000)) == pdTRUE)
            print_csi_sample(&sample);
        if (csi_queue_dropped != reported_drops) {
            reported_drops = csi_queue_dropped;
            printf("CSI_QUEUE_DROPPED: %u\n", reported_drops);
        }
    }
}

void _print_csi_csv_header() {
    char *header_str = (char *) "type,role,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,real_time_set,real_timestamp,len,first_word_invalid,CSI_DATA\n";
    outprintf(header_str);
}

void csi_init(char *type) {
    project_type = type;

#ifdef CONFIG_SHOULD_COLLECT_CSI
    csi_queue = xQueueCreate(32, sizeof(CsiSample));
    configASSERT(csi_queue);
    BaseType_t created = xTaskCreate(csi_writer_task, "csi_writer", 6144, NULL, 4, NULL);
    configASSERT(created == pdPASS);

    // @See: https://github.com/espressif/esp-idf/blob/master/components/esp_wifi/include/esp_wifi_types.h#L401
    wifi_csi_config_t configuration_csi = {};
    configuration_csi.lltf_en = 1;
    configuration_csi.htltf_en = 1;
    configuration_csi.stbc_htltf2_en = 1;
    configuration_csi.ltf_merge_en = 1;
    configuration_csi.channel_filter_en = 0;
    configuration_csi.manu_scale = 0;

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&configuration_csi));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&_wifi_csi_cb, NULL));

    _print_csi_csv_header();
    ESP_ERROR_CHECK(esp_wifi_set_csi(1));
#endif
}

#endif //ESP32_CSI_CSI_COMPONENT_H
